-- scripts/bootstrap_roles.sql
--
-- Run ONCE per cluster, as a superuser, BEFORE `alembic upgrade head`.
--
--   psql "$SUPERUSER_URI" -v app_user=app \
--        -v copilot_hod_role=ci_copilot_hod -v copilot_exec_role=ci_copilot_exec \
--        -v copilot_role=ci_copilot -v loader_role=ci_loader \
--        -f scripts/bootstrap_roles.sql
--
-- Why this is not an alembic migration: creating a role and granting role
-- membership needs CREATEROLE or superuser. The migration user deliberately has
-- neither -- a superuser owns its way past object privileges, so the HOD/EXEC
-- split would stop being enforced with no error at query time.
--
-- Creates NOLOGIN roles that the app switches into per unit of work:
--
--   ci_copilot_hod  -- read path for HOD callers. SELECT on ci_hod only.
--   ci_copilot_exec -- read path for EXEC callers. SELECT on ci_exec only.
--   ci_loader       -- write path. INSERT/UPDATE/DELETE/TRUNCATE.
--   ci_copilot      -- the single read role the two personas replaced. Still
--                      created, because revisions 01/06/07 grant to it by name and
--                      revision 09 revokes those grants by name. Nothing switches
--                      into it; after 09 it holds nothing.
--
-- The persona split is the whole of access control now, and it is object
-- privileges that enforce it: neither read role can reach the other's schema, and
-- neither can reach the fact tables the persona views are built from. The SQL
-- validator rejects the same queries earlier and with a better message; it is no
-- longer the only thing standing there.
--
-- ...and optionally one LOGIN role for humans:
--
--   ci_analyst -- read-only account for analysts, debugging and BI tools. Reads
--                 both persona schemas, because a human debugging an executive's
--                 answer needs to see what the HOD view would have returned.
--                 Created only when -v analyst_password=... is supplied.
--
-- No role is granted to the login user with INHERIT, so the design fails CLOSED:
-- a session that forgets to SET ROLE holds no privilege on anything and gets a
-- permission error, instead of silently getting everything.
--
-- Note: psql does not interpolate :'vars' inside dollar-quoted blocks, so the
-- values are carried into the DO blocks through session settings.
--
-- Idempotent. Safe to re-run.

\set ON_ERROR_STOP on

SELECT set_config('bootstrap.copilot_role', :'copilot_role', false);
SELECT set_config('bootstrap.loader_role', :'loader_role', false);
SELECT set_config('bootstrap.app_user', :'app_user', false);

-- The persona read roles. Defaulted so an operator running the old command line
-- still ends up with a database revision 09 can migrate.
\if :{?copilot_hod_role}
\else
    \set copilot_hod_role ci_copilot_hod
\endif
\if :{?copilot_exec_role}
\else
    \set copilot_exec_role ci_copilot_exec
\endif
SELECT set_config('bootstrap.copilot_hod_role', :'copilot_hod_role', false);
SELECT set_config('bootstrap.copilot_exec_role', :'copilot_exec_role', false);

-- Optional vars. \if needs them defined, so default them to empty first.
\if :{?analyst_role}
\else
    \set analyst_role ci_analyst
\endif
\if :{?analyst_password}
\else
    \set analyst_password ''
\endif
SELECT set_config('bootstrap.analyst_role', :'analyst_role', false);
SELECT set_config('bootstrap.analyst_password', :'analyst_password', false);

-- ---------------------------------------------------------------------------
-- The roles the app switches into via SET LOCAL ROLE.
--
-- NOLOGIN: nobody authenticates as any of them directly.
-- NOBYPASSRLS: kept although there are no policies left to bypass. It costs
-- nothing, and a read role that could bypass RLS is a role somebody could later
-- reinstate policies around and not notice was exempt.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_role text;
BEGIN
    FOREACH v_role IN ARRAY ARRAY[
        current_setting('bootstrap.copilot_hod_role'),
        current_setting('bootstrap.copilot_exec_role'),
        current_setting('bootstrap.copilot_role'),
        current_setting('bootstrap.loader_role')
    ] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
            EXECUTE format('ALTER ROLE %I NOLOGIN NOBYPASSRLS', v_role);
            RAISE NOTICE 'role % already existed; enforced NOLOGIN NOBYPASSRLS', v_role;
        ELSE
            EXECUTE format('CREATE ROLE %I NOLOGIN NOBYPASSRLS', v_role);
            RAISE NOTICE 'created role %', v_role;
        END IF;
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- The application login user must be able to SET ROLE into each of them -- but
-- must NOT inherit their privileges automatically.
--
-- This matters more than it looks, and it matters more now than it did. An
-- inheriting member holds the role's privileges with no SET ROLE at all, so the
-- login user would hold SELECT on BOTH persona schemas at once -- and the entire
-- HOD/EXEC boundary is which schema the session's role may read. A request that
-- never entered copilot_scope() would then read the HOD views as the login user
-- rather than failing.
--
-- A non-inheriting grant means privileges take effect only after an explicit
-- SET ROLE. The login user on its own can read nothing. The design fails closed,
-- and revision 09 re-checks this before it grants anything.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_role  text;
    v_app   text := current_setting('bootstrap.app_user');
    v_pgver integer := current_setting('server_version_num')::integer;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_app) THEN
        RAISE EXCEPTION 'application role % does not exist; create it first', v_app;
    END IF;

    FOREACH v_role IN ARRAY ARRAY[
        current_setting('bootstrap.copilot_hod_role'),
        current_setting('bootstrap.copilot_exec_role'),
        current_setting('bootstrap.copilot_role'),
        current_setting('bootstrap.loader_role')
    ] LOOP
        IF v_pgver >= 160000 THEN
            -- Per-grant control: surgical, leaves other memberships alone.
            EXECUTE format('GRANT %I TO %I WITH INHERIT FALSE', v_role, v_app);
        ELSE
            EXECUTE format('GRANT %I TO %I', v_role, v_app);
        END IF;
        RAISE NOTICE 'granted % to % (inherit=false)', v_role, v_app;
    END LOOP;

    IF v_pgver < 160000 THEN
        -- Pre-16 has no per-grant INHERIT, so make the login role itself
        -- non-inheriting. Broader, but achieves the same fail-closed property.
        EXECUTE format('ALTER ROLE %I NOINHERIT', v_app);
        RAISE NOTICE 'PostgreSQL < 16: set % NOINHERIT (no per-grant INHERIT support)', v_app;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Verify it actually took effect, rather than trusting the grant.
--
-- Every switchable role, not just the loader. The persona roles are the ones that
-- matter most: if the login user inherits both, it can read ci_hod and ci_exec at
-- the same time and the split is decorative.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_app  text := current_setting('bootstrap.app_user');
    v_role text;
BEGIN
    FOREACH v_role IN ARRAY ARRAY[
        current_setting('bootstrap.copilot_hod_role'),
        current_setting('bootstrap.copilot_exec_role'),
        current_setting('bootstrap.copilot_role'),
        current_setting('bootstrap.loader_role')
    ] LOOP
        IF pg_has_role(v_app, v_role, 'USAGE') THEN
            RAISE EXCEPTION
                '% still inherits % (pg_has_role USAGE is true), so it holds that '
                'role''s privileges with no SET ROLE -- a session that forgot to '
                'switch roles would read as % instead of failing. Fix with: '
                'GRANT %I TO %I WITH INHERIT FALSE;  -- or ALTER ROLE %I NOINHERIT;',
                v_app, v_role, v_role, v_role, v_app, v_app;
        END IF;
    END LOOP;

    RAISE NOTICE 'verified % inherits none of the switchable roles; SET ROLE is required', v_app;
END $$;

-- ---------------------------------------------------------------------------
-- Hard check: the two persona roles must be separate roles, and neither may be a
-- member of the other.
--
-- A membership either way collapses the boundary silently: ci_copilot_exec as a
-- member of ci_copilot_hod holds SELECT on ci_hod, and every query an executive
-- asks would be answered with money in it and nothing would report a fault.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_hod  text := current_setting('bootstrap.copilot_hod_role');
    v_exec text := current_setting('bootstrap.copilot_exec_role');
BEGIN
    IF v_hod = v_exec THEN
        RAISE EXCEPTION
            'copilot_hod_role and copilot_exec_role are both %. They must be two '
            'roles, or every caller reads the same persona schema.', v_hod;
    END IF;

    IF pg_has_role(v_hod, v_exec, 'USAGE') OR pg_has_role(v_exec, v_hod, 'USAGE') THEN
        RAISE EXCEPTION
            '% and % are members of one another, so both read both persona '
            'schemas and the HOD/EXEC split is not enforced. Fix with: '
            'REVOKE %I FROM %I;', v_hod, v_exec, v_hod, v_exec;
    END IF;

    RAISE NOTICE 'verified % and % are independent roles', v_hod, v_exec;
END $$;

-- ---------------------------------------------------------------------------
-- Hard check: the application user must not be a superuser.
--
-- The HOD/EXEC split is object privileges and nothing else now. A superuser is
-- not subject to them, so a superuser login user reads ci_hod whichever role it
-- switched into -- with no error at query time, which is the failure mode worth
-- refusing to start over.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_app text := current_setting('bootstrap.app_user');
    v_bad boolean;
BEGIN
    SELECT rolsuper OR rolbypassrls INTO v_bad FROM pg_roles WHERE rolname = v_app;

    IF v_bad THEN
        RAISE EXCEPTION
            'application role % is SUPERUSER or has BYPASSRLS. A superuser is not '
            'subject to the GRANTs that separate ci_hod from ci_exec, so the money '
            'split would not be enforced -- with no error at query time. Fix with: '
            'ALTER ROLE %I NOSUPERUSER NOBYPASSRLS;', v_app, v_app;
    END IF;

    RAISE NOTICE 'verified % is NOSUPERUSER and NOBYPASSRLS', v_app;
END $$;

-- ---------------------------------------------------------------------------
-- Optional: the read-only human role.
--
-- Created only when a password is supplied, so the default bootstrap stays
-- exactly as it was:
--
--     psql "$SUPERUSER_URI" -v app_user=app -v analyst_password='...' \
--          -f scripts/bootstrap_roles.sql
--
-- Deliberately NOBYPASSRLS and NOSUPERUSER. Its visibility comes from explicit
-- SELECT grants issued by the migrations, which are auditable on the object
-- (`\dp ci_hod.v_sales_daily`) and reach only what was granted. A superuser
-- account would read everything in every database and show up nowhere.
--
-- Deliberately NOT granted membership in ci_loader: that would carry
-- INSERT/UPDATE/DELETE/TRUNCATE with it, so a "read-only" account would quietly
-- hold write access to every fact table.
--
-- Deliberately NOT granted membership in either persona role either. It gets its
-- own SELECT on both persona schemas from the migration, which is visible in
-- \dp output; membership would make "why can the analyst see money" a question
-- about role graphs rather than about one grant.
--
-- Table privileges are granted by the migration, not here: the tables do not
-- exist yet at bootstrap time.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_role     text := current_setting('bootstrap.analyst_role');
    v_password text := current_setting('bootstrap.analyst_password');
BEGIN
    -- The role always exists, so the migrations can grant to it unconditionally
    -- and every database ends up with the same privileges. Without a password it
    -- stays NOLOGIN and nobody can use it; supplying one later is all it takes to
    -- enable the account, because the grants are already in place.
    IF v_password = '' THEN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
            EXECUTE format(
                'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOBYPASSRLS NOREPLICATION', v_role
            );
            RAISE NOTICE
                'created % as NOLOGIN. Pass -v analyst_password=... to enable it '
                'as a read-only human account.', v_role;
        ELSE
            -- Never downgrade an account somebody is already using; only
            -- re-assert the attributes that make it read-only.
            EXECUTE format(
                'ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOBYPASSRLS NOREPLICATION', v_role
            );
            RAISE NOTICE 'role % already existed; re-asserted read-only attributes', v_role;
        END IF;
        RETURN;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
        EXECUTE format(
            'ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE '
            'NOBYPASSRLS NOREPLICATION', v_role, v_password
        );
        RAISE NOTICE 'role % already existed; password and attributes reset', v_role;
    ELSE
        EXECUTE format(
            'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE '
            'NOBYPASSRLS NOREPLICATION', v_role, v_password
        );
        RAISE NOTICE 'created read-only role %', v_role;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Let the application login user SET ROLE into the analyst as well -- again
-- WITHOUT inheriting it.
--
-- This is purely an operator convenience: one connection (psql, DBeaver) can
-- switch between ci_analyst for read-only browsing, either persona role for
-- reproducing what a caller saw, and ci_loader for a load, instead of needing
-- four sets of credentials.
--
-- INHERIT FALSE is not optional here. The analyst holds SELECT on BOTH persona
-- schemas, so an inheriting login user would hold it too -- and the app connects
-- as that login user. A request that missed copilot_scope() would then read the
-- HOD views, money included, instead of failing. The check below enforces it.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_role  text := current_setting('bootstrap.analyst_role');
    v_app   text := current_setting('bootstrap.app_user');
    v_pgver integer := current_setting('server_version_num')::integer;
BEGIN
    IF v_pgver >= 160000 THEN
        EXECUTE format('GRANT %I TO %I WITH INHERIT FALSE', v_role, v_app);
    ELSE
        EXECUTE format('GRANT %I TO %I', v_role, v_app);
    END IF;
    RAISE NOTICE 'granted % to % (inherit=false)', v_role, v_app;
END $$;

-- ---------------------------------------------------------------------------
-- Hard check: the analyst must be read-only in fact, not just by intention,
-- and the login user must not inherit it.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_role   text := current_setting('bootstrap.analyst_role');
    v_loader text := current_setting('bootstrap.loader_role');
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION
            'role % is SUPERUSER or has BYPASSRLS. Its read access must come from '
            'the explicit SELECT policy so it stays visible on the table and '
            'scoped to the tables granted. Fix with: '
            'ALTER ROLE %I NOSUPERUSER NOBYPASSRLS;', v_role, v_role;
    END IF;

    IF pg_has_role(v_role, v_loader, 'USAGE') THEN
        RAISE EXCEPTION
            'role % inherits %, so it holds INSERT/UPDATE/DELETE/TRUNCATE on every '
            'fact table -- it is not read-only. Fix with: REVOKE %I FROM %I;',
            v_role, v_loader, v_loader, v_role;
    END IF;

    IF pg_has_role(current_setting('bootstrap.app_user'), v_role, 'USAGE') THEN
        RAISE EXCEPTION
            '% inherits %, which holds SELECT on both persona schemas -- so % '
            'reads the HOD views without any SET ROLE, and a request that skipped '
            'copilot_scope() would return money to an executive. Fix with: '
            'GRANT %I TO %I WITH INHERIT FALSE;',
            current_setting('bootstrap.app_user'), v_role,
            current_setting('bootstrap.app_user'), v_role,
            current_setting('bootstrap.app_user');
    END IF;

    RAISE NOTICE 'verified % is read-only: NOSUPERUSER, not a member of %, and '
        'not inherited by %', v_role, v_loader,
        current_setting('bootstrap.app_user');
END $$;
