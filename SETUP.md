# Initialization runbook — v3 serving schema

Verified end-to-end against `postgres:16`. No third-party extensions required.

## 0. Prerequisites

- PostgreSQL **15+** (16 recommended). Revision `20260727_07` refuses to run
  below 15. That check was written for `security_invoker`, which the persona views
  no longer use — see revision `20260828_09` — and it has been left in place
  rather than lowered, because nothing has been tested below 15.
- On 15, per-grant `INHERIT FALSE` is unavailable and `bootstrap_roles.sql` falls
  back to making the login role `NOINHERIT`. Same effect, broader blast radius.
- `pg_trgm` is required (ships with the standard image). `ltree` is optional —
  without it `category_path` becomes `text` and only subtree operators are lost.

> **Access model.** There is no row-level security and no OpCo or category
> scoping: every caller sees every OpCo and every product category. The one
> control is the **HOD/EXEC money split**, and it is enforced by object
> privileges — `ci_copilot_hod` can read `ci_hod` and nothing else,
> `ci_copilot_exec` can read `ci_exec` and nothing else, and neither can read the
> fact tables the views are built from. Revision `20260828_09` establishes the
> persona roles; anything below describing OpCo isolation is out of date, so
> report it rather than working around it.

## 1. Create the login role — once per cluster, as superuser

```bash
psql "$SUPERUSER_URI" <<'SQL'
CREATE ROLE app LOGIN PASSWORD '<secret>' NOSUPERUSER NOBYPASSRLS NOCREATEDB;
CREATE DATABASE customer_intelligence OWNER app;
SQL
psql "$SUPERUSER_URI/customer_intelligence" -c "GRANT ALL ON SCHEMA public TO app;"
```

`NOSUPERUSER` is not optional. A superuser is not subject to the `GRANT`s that
separate `ci_hod` from `ci_exec`, so the money split would stop being enforced
**with no error at query time**. `bootstrap_roles.sql` asserts this and refuses to
proceed otherwise. `NOBYPASSRLS` is kept for the same belt-and-braces reason the
roles are: there are no policies today, and a role exempt from ones added later is
a role nobody would notice was exempt.

## 2. Create the roles — once per cluster, as superuser

```bash
psql "$SUPERUSER_URI/customer_intelligence" \
     -v app_user=app \
     -v copilot_hod_role=ci_copilot_hod -v copilot_exec_role=ci_copilot_exec \
     -v copilot_role=ci_copilot -v loader_role=ci_loader \
     -f scripts/bootstrap_roles.sql
```

| Role | Login | Reads | Writes |
|---|---|---|---|
| `ci_copilot_hod` | no | `ci_hod` views, the four dimensions, `ci_meta` catalogues | no |
| `ci_copilot_exec` | no | `ci_exec` views, the four dimensions, `ci_meta` catalogues | no |
| `ci_loader` | no | everything | yes |
| `ci_analyst` | only with a password | both persona schemas and `ci_core` | no |
| `ci_copilot` | no | nothing after `20260828_09` | no |

The two persona roles **are** the access control. Neither holds any privilege on
`ci_core.fact_sales_daily` or the other aggregates — the persona views run with
their owner's privileges, so the caller needs none — and neither can reach the
other's schema, because `USAGE` on it was never granted. An executive session
asking for `gross_sales_amount` therefore has three separate things to get past:
the column is not in its view, the view that has it is not readable by its role,
and the table under that is not readable either.

`ci_copilot` is the single read role the two replaced. It is still created,
because revisions 01/06/07 grant to it by name and revision 09 revokes those
grants by name. Nothing switches into it.

Every role is granted to `app` **with `INHERIT FALSE`**, which matters more than
it looks. An inheriting member holds the role's privileges with no `SET ROLE` at
all — so `app` would hold `SELECT` on **both** persona schemas at once, and a
request that skipped `copilot_scope()` would read the HOD views instead of
failing. Revision 09 re-checks this before it grants anything and refuses to run
if it is wrong.

This step needs `CREATEROLE`, which is exactly why it is not an alembic migration.

### Querying the data yourself

`app` owns the tables, so a plain `SELECT` as `app` reads everything. That is not
a hole — `app` is the migration and ownership account, it holds no membership it
inherits, and nothing the copilot generates ever runs as it. But it does mean
**ad-hoc queries as `app` tell you nothing about what a caller can see.** To check
that, use the role the caller would have used.

Use `ci_analyst` for browsing. Give it a password to enable it:

```bash
psql "$SUPERUSER_URI/customer_intelligence" \
     -v app_user=app -v analyst_password='...' \
     -f scripts/bootstrap_roles.sql
```

```bash
psql "postgresql://ci_analyst:...@localhost:5432/customer_intelligence"
SELECT count(*) FROM ci_core.fact_sales_daily;   -- works, no SET ROLE needed
SELECT count(*) FROM ci_hod.v_sales_daily;       -- works: analysts see both personas
```

Its access comes from explicit `SELECT` grants issued by the migrations — visible
in `\dp ci_hod.v_sales_daily` — rather than the `BYPASSRLS` or `SUPERUSER`
attributes, which would apply cluster-wide to every database and every future
table and appear nowhere in the schema. It holds `SELECT` and nothing else: writes,
DDL and `SET ROLE ci_loader` are all denied, and `bootstrap_roles.sql` fails if the
role ever gains `SUPERUSER` or membership in `ci_loader` (which would carry write
privileges with it).

### One connection, four personas

`app` can `SET ROLE` into all of them, so a single DBeaver/psql profile covers
every case. Put the switch in the client's connection-init SQL:

| `SET ROLE` … | sees | writes |
|---|---|---|
| *(nothing)* | everything — `app` owns the tables | yes (it is the owner) |
| `ci_copilot_hod` | `ci_hod` views, money included | no |
| `ci_copilot_exec` | `ci_exec` views, no money columns at all | no |
| `ci_analyst` | both persona schemas and `ci_core` | no |
| `ci_loader` | everything | **yes** |

Every membership is granted `WITH INHERIT FALSE`, which is load-bearing for the
persona roles above all: an inheriting grant would give plain `app` sessions
`SELECT` on both `ci_hod` and `ci_exec` at once, and a request that missed
`copilot_scope()` would answer an executive's question with money in it.
`bootstrap_roles.sql` asserts `pg_has_role('app', <role>, 'USAGE')` is false for
every switchable role.

**Keep the client in auto-commit.** `SET ROLE` is transactional: in manual-commit
mode one failed query plus a rollback silently reverts the session to `app`, and
every subsequent query returns 0 rows with no error explaining why.

Two alternatives, no setup required:

```bash
# superuser — not subject to any of the grants above
docker compose exec postgres psql -U postgres -d customer_intelligence

# set the role at connect time, no DDL
PGOPTIONS="-c role=ci_loader" psql "postgresql://app:app@localhost:5432/customer_intelligence"
```

To reproduce exactly what one caller sees, set the matching role, search_path and
the two GUCs. There were six: `app.opco_codes`, `app.category_keys` and
`app.is_group_user` existed only to be read by RLS predicates and went with them,
and `app.min_cell_size` drove small-cell suppression, which is also gone.

```sql
-- A HOD caller
SET ROLE ci_copilot_hod;
SELECT set_config('app.principal_id','edward',false),
       set_config('app.role_level','HOD',false);
SET search_path TO ci_hod, ci_core, ci_meta;
SELECT SUM(gross_sales_amount) FROM v_sales_daily;   -- works

-- The same question as an EXEC caller
RESET ROLE; SET ROLE ci_copilot_exec;
SET search_path TO ci_exec, ci_core, ci_meta;
SELECT SUM(gross_sales_amount) FROM v_sales_daily;   -- ERROR: column does not exist
SELECT SUM(gross_sales_amount) FROM ci_hod.v_sales_daily;  -- ERROR: permission denied
SELECT SUM(gross_sales_amount) FROM ci_core.fact_sales_daily; -- ERROR: permission denied
```

Those three errors are the access model. If any of them returns a number instead,
stop and re-check step 2 — the `INHERIT FALSE` grants in particular.

## 3. Schema

```bash
export POSTGRES_URI="postgresql://app:<secret>@localhost:5432/customer_intelligence"
alembic upgrade head          # -> 20260828_09
```

Creates 4 schemas, 9 tables, monthly partitions, and 6 persona views in each of
`ci_hod` and `ci_exec` — owner-executed, each readable by exactly one persona role.
No RLS policies: none are created, and access control is the persona views plus
the per-role grants in revisions `20260727_06` and `20260828_09`.

## 4. Reference data and metadata

Order matters — the facts reference real category and store keys.

```bash
python -m app.scripts.load_reference_data       # dims + lookup catalog
python -m app.scripts.load_glossary             # governed column glossary
python -m app.scripts.load_metric_definitions   # metric registry
```

Expected output:

```
dim_opco                     : 5
dim_product_category         : 5240  by level {1: 16, 2: 57, 3: 312, 4: 4855}
dim_product_category_closure : 20486
dim_store                    : 552
copilot_lookup_value         : 5792
Loaded 260 glossary rows into ci_meta.copilot_glossary (14 HOD-only, 3 identity)
Loaded 27 metrics into ci_meta.metric_definition (10 HOD-only, 17 available to all roles)
```

**Category keys are deterministic** — nodes sorted by `(opco, level, path)`,
numbered from 1000 — so a key stored anywhere outside this database (a saved SQL
snippet, a dashboard filter) stays valid across reloads. Changing that sort
invalidates all of them.

## 5. Mock data (dev only)

```bash
python scripts/generate_mock_data.py --months 6 --customers 2000
```

Seeded and deterministic. Shaped so the questions have visibly right answers:
Evening outsells Morning/Afternoon, penetration sits near 70%, weekends run ~35%
above weekdays, and ~18% of customers also hold NORTHCO_CREDIT so cross-OpCo overlap
returns a real number rather than a zero that belongs to the generator.

## 6. Verify

```sql
BEGIN;
SET LOCAL ROLE ci_copilot_hod;
SELECT set_config('app.principal_id','edward',true),
       set_config('app.role_level','HOD',true);
SET LOCAL search_path = ci_hod, ci_core, ci_meta;

SELECT SUM(transaction_count) AS txn,
       ROUND(100.0*SUM(member_transaction_count)/NULLIF(SUM(transaction_count),0),1) AS pen_pct
FROM v_sales_summary_daily
WHERE calendar_date BETWEEN DATE '2026-06-03' AND DATE '2026-06-22';
COMMIT;
```

Use `set_config(..., true)` **inside a transaction**. It is transaction-local, so
a psql session in autocommit discards it after each statement — and
`assert_scope_active()` then refuses to run anything, which is the behaviour you
want but is confusing if you did not expect it.

Sanity check that the persona split is live. Every one of these must be an
**error**, not a row:

```sql
BEGIN;
SET LOCAL ROLE ci_copilot_exec;
SELECT count(*) FROM ci_hod.v_sales_daily;        -- permission denied for schema ci_hod
SELECT count(*) FROM ci_core.agg_sales_daily;     -- permission denied for table
ROLLBACK;

BEGIN;
SET LOCAL ROLE ci_copilot_hod;
SELECT count(*) FROM ci_exec.v_sales_daily;       -- permission denied for schema ci_exec
SELECT count(*) FROM ci_core.fact_sales_daily;    -- permission denied for table
ROLLBACK;
```

If any of them returns a count, the split is not enforced: re-check step 2, and in
particular that `app` does not inherit either persona role. Revision `20260828_09`
runs the same checks with `has_table_privilege()` and fails the migration rather
than leaving the database in that state, so seeing rows here means something was
changed after it ran.

## 7. Application config

```bash
POSTGRES_URI=postgresql://app:<secret>@postgres:5432/customer_intelligence
CI_ACCESS_LOG_ENABLED=true     # authorization audit rows
GLOSSARY_SOURCE=postgres       # falls back to the seed CSV if the table is empty
```

Every request must carry a `permission` block:

```json
{
  "query": "sales and membership penetration 3 to 22 June",
  "permission": {
    "principal_id": "user-123",
    "role_level": "HOD"
  }
}
```

- `principal_id` — the real caller. Recorded in `ci_meta.copilot_access_log`, which
  is the only place it is stored: MLflow keeps `safe_user_hash(user_id)`, so the
  trace alone cannot answer which person ran a query.
- `role_level` — `HOD` or `EXEC`, and the whole of access control. It picks the
  database role the request runs as **and** the schema on its `search_path`:
  `HOD` -> `ci_copilot_hod` reading `ci_hod`, `EXEC` -> `ci_copilot_exec` reading
  `ci_exec`, where the money columns do not exist. A query touching one as an
  executive fails loudly instead of returning zeros, and the role it runs as cannot
  read the HOD view or the fact table either.

**Not part of the permission block.** An earlier design carried `opco_codes`,
`category_keys`, `category_names` and `is_group_user`, and access was scoped by all
of them. There is no OpCo or category scoping now — every caller sees every OpCo
and every product category.

Those fields are **rejected, not ignored**: `PermissionContext` sets
`extra="forbid"`, so a caller still sending one gets a `422` naming it. That is
deliberate. An authorization payload is the last place to silently drop a field
nobody recognises, and a block that reads as narrowly scoped while being enforced
as unrestricted is exactly the failure the old `is_group_user` flag caused — one
mis-set boolean granting everything, cross-checked against nothing.

## Reload procedures

| Change | Command |
|---|---|
| Product hierarchy or stores changed | `load_reference_data` — **re-check stored `category_keys`** |
| Glossary CSV changed | `load_glossary` |
| Metric definitions changed | `load_metric_definitions` |
| New months of data | extend partitions via `CI_PARTITION_MONTHS_FORWARD`, re-run migration 03 |

## Known limitations

- **Distinct customer counts are monthly.** The customer tables are monthly, so a
  sub-month period cannot give an exact customer count. Use transaction-based
  penetration for arbitrary date ranges and say the figure is transaction-based.
- **A question about a category deeper than level 2** cannot be answered by
  `v_sales_summary_daily` — its deepest column is `category_l2_key`, and a level-2
  rollup row aggregates siblings the question did not ask about. The planner routes
  to `v_sales_daily` instead and says which level it answered at.
- **`ci_copilot` still exists and holds nothing.** Revision `20260828_09` revoked
  every grant revisions 01/06/07 gave it. Dropping the role is a cluster operation
  this repo does not perform; leaving it is harmless, and it keeps those revisions
  replayable on a fresh database.
- **`store_id` is unique only within an OpCo.** NORTHCO 1011 and NORTHCO_MART 1011 are
  different stores, so `dim_store` is keyed `(opco_code, store_id)` and every join
  must match both columns.
- **No `postgres_hll`.** Distinct customer counts are exact rather than
  approximate, at the cost of monthly grain.
