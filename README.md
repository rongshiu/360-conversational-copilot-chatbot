# copilot-customer-intelligence

A FastAPI-based customer intelligence service built with async SQLAlchemy, PostgreSQL, Alembic migrations, and modular app structure.

## Project structure

Root files and config
- `.env` / `.env.example` - environment variables: Postgres URIs, roles, Vertex/Gemini and MLflow settings.
- `Dockerfile` - container image build definition.
- `docker-compose.yml` - local compose orchestration for the service.
- `pyproject.toml` - Python package metadata and dependencies.
- `alembic.ini` - Alembic migration configuration.
- `uv.lock` - dependency lock file.

Main source package
- `app/main.py` - application entrypoint and FastAPI app setup.
- `app/agents/` - planner, SQL validator, analysis and answer agents.
- `app/core/` - configuration, constants, logging, and shared core behavior.
- `app/db/` - engine, SQLAlchemy `Base`, v3 DDL constants, and `session_scope.py`
  (the `SET ROLE` + GUC scope every request runs inside).
- `app/graph/` - LangGraph nodes and the copilot state machine.
- `app/lifecycles/` - lifecycle helpers and lifespan management.
- `app/middleware/` - custom middleware modules.
- `app/models/` - request/response schemas and the generated v3 ORM models
  (`app/models/schemas/v3_core.py`, `v3_meta.py`).
- `app/policies/` - clarification policy.
- `app/routes/` - API routes and routers.
  - `app/routes/healths.py` - health check endpoints (`/healthz`).
  - `app/routes/v1/copilot.py` - v1 Copilot API routes.
- `app/service/` - principal resolution, entity resolution, glossary, table
  registry, SQL execution, currency, and the SSE
  stream context.
- `app/utils/` - shared utility functions.
- `scripts/` - operator scripts: `bootstrap_roles.sql`, `generate_mock_data.py`,
  `generate_orm_models.py`, `eval_copilot.py`.

Companion docs
- `SETUP.md` - roles, personas, and the permission block field by field.
- `TESTING.md` - the three test levels, the end-to-end smoke questions with their
  expected answers per role, and what to check when an answer looks like `0`.
- `copilot_ask_flow.md` - the LangGraph flow: scope layer, every node and branch,
  the streaming sequence, and which worker at each step is an LLM vs a guard.

## API

Two endpoints answer questions, over the same graph and the same guards. Both require
an `x-user-id` header.

| Endpoint | Returns |
|---|---|
| `POST /v1/copilot/ask` | one JSON response |
| `POST /v1/copilot/ask/stream` | the same response, streamed as SSE |

```bash
curl -s localhost:8080/v1/copilot/ask \
  -H 'Content-Type: application/json' -H 'x-user-id: edward' \
  -d '{
    "query": "show me sales by product division for june 2026",
    "thread_id": null,
    "permission": {
      "principal_id": "edward",
      "role_level": "HOD"
    }
  }'
```

### The permission block

Every request carries its own authorization. Nothing is inferred from the
`x-user-id` header, and there is no ambient session identity.

| Field | Meaning |
|---|---|
| `principal_id` | The real caller, recorded in `ci_meta.copilot_access_log` |
| `role_level` | `HOD` sees revenue; `EXEC` reads views where money columns are **physically absent** |

Those two fields are the whole block, and `role_level` is the whole of access
control. It selects both the database role the request runs as and the schema on
its `search_path`: `HOD` -> `ci_copilot_hod` reading `ci_hod`, `EXEC` ->
`ci_copilot_exec` reading `ci_exec`. Neither role can read the other's schema or
the fact tables underneath, so an executive cannot reach a revenue column even if
the generated SQL asks for one.

**Not part of this block.** An earlier design carried `opco_codes`,
`category_keys`, `category_names` and `is_group_user`, and access was scoped by all
of them. It is not any more — every caller sees every OpCo and every product
category. The fields are **rejected**, not ignored: sending one is a `422` naming
it. A payload that lies about what will be enforced is worse than one that breaks
loudly.

A permission block that cannot be honoured is a **422** from the schema (an unknown
`role_level`, a retired field, a missing `principal_id`) or a **403** if resolution
fails for any other reason. Guessing is either a leak or a silent narrowing, so it
fails at the edge instead.

### Response shape

| Field | Notes |
|---|---|
| `type` | `analytics` · `glossary` · `clarify` · `out_of_scope` · `unsupported` · `smalltalk` · `error` |
| `answer` | The narrative. Monetary figures are `RM` — see **Currency** below |
| `chart` | `ChartSpec`: `chart_type`, `value_format`, `currency`, `data` … |
| `rows` | Result rows, capped at `MAX_QUERY_ROWS` by the database |
| `sql` | The executed SQL, for auditing the answer |
| `lookup_matches` / `lookup_context` / `lookup_plan` | Which entities resolved to what, so an answer is never a silent guess about a store or category |
| `analysis` | The structured reasoning behind `answer`: finding, calculation logic, limitations |
| `policies` | Every authorization policy that shaped this response — see **Policy notices** below |
| `thread_id` / `request_id` | Pass `thread_id` back to continue the conversation |

### Policy notices

An empty result and a refusal look identical to a client that only reads `answer`,
which is the confident-zero failure moved up a layer. So every policy that changed
what came back is also reported structurally, in `policies`:

```json
{
  "code": "role_money_withheld",
  "effect": "substituted",
  "enforced_by": "persona_view.ci_exec",
  "message": "Revenue measures are not available at your role level, so this answer uses a volume measure instead. The figure is not money.",
  "scope": { "role_level": "EXEC" },
  "requested_metric": "total_sales",
  "substituted_with": "total_transactions"
}
```

Branch on **`effect`**. It is reported whichever way the policy went, because two of
the three outcomes are not refusals:

| `effect` | Meaning |
|---|---|
| `denied` | Refused. `type` is `out_of_scope` |
| `substituted` | Answered, but with a different measure than the one asked for |

| `code` | `enforced_by` | Raised when |
|---|---|---|
| `customer_identity` | `schema.no_customer_key` | The question asks to identify, list or give examples of individuals, which no grain answers. Classified by the planner, so any phrasing is caught — not a pattern list |
| `role_money_withheld` | `persona_view.ci_exec` | An `EXEC` asked for revenue. `substituted` when the metric registry has a volume twin, `denied` when it does not — `avg_basket_size_value` has no honest equivalent, since units per basket is a different quantity |

Three codes were removed and a client branching on them can delete those arms.
`opco_out_of_scope` and `category_out_of_scope` went with row-level security —
every caller sees every OpCo and every category. `small_cell_suppressed` went with
small-cell suppression, which blanked counts of fewer than `CI_MIN_CELL_SIZE`
customers; nothing blanks a count now, so the `suppressed` effect is gone too.

`code` is stable and safe to key on; `message` is written for a person and may be
reworded. A notice describes what happened to the **answer**; it is not a channel
for enumerating the catalogue. `scope` is `{"role_level": ...}` — that is the whole
of the caller's grant now.

`denied_reason` remains on `out_of_scope` responses for compatibility. It carries the
same sentence as the matching notice's `message`.

**Not every refusal is an authorization event.** A question the schema cannot answer
for *anyone* — brand affinity per customer, a list of individual customers — returns
`unsupported` with an **empty** `policies`. Only a refusal a different permission
grant would have lifted produces a notice. That distinction is what tells a client
whether "request access" is worth offering, and it is classified by the planner via
`SqlPlan.unsupported_cause` rather than inferred from the wording of the reason.

### Streaming

```bash
curl -sN localhost:8080/v1/copilot/ask/stream \
  -H 'Content-Type: application/json' -H 'x-user-id: edward' \
  -d '{"query":"show me sales by product division for june 2026","thread_id":null,
       "permission":{"principal_id":"edward","role_level":"HOD"}}'
```

`curl -N` matters — without it curl buffers and every event appears at once, which
looks like broken streaming.

| Event | Payload |
|---|---|
| `status` | Progress: understanding, resolving entities, planning, validating, running, interpreting |
| `delta` | `{"text": "..."}` — real LLM tokens for `analytics`; one delta carrying the whole answer for every other type, so a client renders both the same way |
| `meta` | `thread_id`, `request_id`, `type`, `intent_reason` |
| `final` | The complete `/ask` payload |
| `done` | `{"ok": true}` |
| `error` | Terminal failure. Distinct from a `403`, which arrives as a real status code |
| `: keepalive` | An SSE **comment**, sent after 10s of silence. Conforming clients ignore it |

Order is `status`* → `delta`* → `meta` → `final` → `done`. Two properties the client
can rely on: a permission fault is a real `403` before any byte of the body is
written, and hanging up cancels the run rather than orphaning it.

## Alembic migrations

This project uses Alembic to manage database schema migrations for PostgreSQL.

### How Alembic is configured
- `alembic.ini` points to `alembic/` as the migration script location.
- `alembic/env.py` loads the app settings and uses `settings.postgres_uri` as the database URL.
- `app/db/postgres.py` exposes the SQLAlchemy `Base` metadata used by Alembic.

**Do not use `--autogenerate`.** The revisions are hand-written SQL because
partitioned parents, owner-executed persona views and per-role grants
have no declarative form in SQLAlchemy metadata — autogenerate cannot see them and
would emit a migration that drops them. `tests/test_schema_drift.py` covers the
one risk that creates, models falling out of step with the schema; regenerate them
with `python scripts/generate_orm_models.py`.

### Common commands

From the `apps/copilot-customer-intelligence` directory:

- Create a new migration (hand-written, see above):
  - `alembic revision -m "describe change"`
- Apply all pending migrations:
  - `alembic upgrade head`
- Roll back one revision:
  - `alembic downgrade -1`
- Show current revision:
  - `alembic current`

### Environment setup

Before running Alembic, set up your `.env` file (copy `.env.sample`). The key value is:

- `POSTGRES_URI`

The package config loads these values from `.env` in `app/core/config.py`.

## Testing

```bash
uv run pytest tests/ -q                                    # unit, no database

POSTGRES_URI=postgresql://app:app@localhost:5432/customer_intelligence \
  uv run pytest tests/ -q                                  # + schema drift and seed data
```

DB-dependent tests **skip** rather than fail without a database, so a green run of the
first command does not mean the schema and data are right. `TESTING.md` has the full
guide, including the end-to-end smoke questions with their expected answers per role,
and what to check when a query returns 0 -- empty, refused and unseeded all look
identical in the response and mean very different things.

## Querying the data directly

There is **no row-level security and no OpCo or category scoping**. Every caller
sees every OpCo and every product category. The one access control is the HOD/EXEC
money split, and it is enforced by object privileges rather than by policies or by
the SQL validator:

| Role | Can read | Cannot read |
|---|---|---|
| `ci_copilot_hod` | `ci_hod` views, the four dimensions, `ci_meta` catalogues | `ci_exec`, every fact table |
| `ci_copilot_exec` | `ci_exec` views (no money columns exist there), same dimensions and catalogues | `ci_hod`, every fact table |

`app` owns the tables, so a plain `SELECT` as `app` reads everything — it is the
migration and ownership account and nothing the copilot generates runs as it.
Which also means **ad-hoc queries as `app` tell you nothing about what a caller can
see.** `SET ROLE` into the role that matches what you want to check. `app` is a
member of all of them, so one connection covers every case — **pick one**:

```sql
-- (a) browse everything, read-only        <- the usual choice
SET ROLE ci_analyst;
SET search_path TO ci_core, ci_meta;

SELECT count(*) FROM fact_sales_daily;   -- 27,512
```

```sql
-- (b) reproduce exactly what one caller sees.
-- Two GUCs, not six. app.opco_codes, app.category_keys and app.is_group_user
-- existed only to be read by RLS predicates and went with them;
-- app.min_cell_size drove small-cell suppression, which is also gone.
SET ROLE ci_copilot_hod;                               -- or ci_copilot_exec
SELECT set_config('app.principal_id','edward',false);
SELECT set_config('app.role_level','HOD',false);       -- must match the role above
SET search_path TO ci_hod, ci_core, ci_meta;           -- ci_exec for the exec role

SELECT SUM(gross_sales_amount) FROM v_sales_daily;     -- works as HOD
```

```sql
-- (c) writes (loaders, ETL)
SET ROLE ci_loader;
```

Switching between them inglegate-session is fine — `SET ROLE` is authorised against the
*session* user, which stays `app`. `RESET ROLE;` returns to `app`.

The split is worth checking rather than trusting. As `ci_copilot_exec`, all three
of these are errors:

```sql
SELECT gross_sales_amount FROM v_sales_daily;              -- column does not exist
SELECT gross_sales_amount FROM ci_hod.v_sales_daily;       -- permission denied for schema
SELECT gross_sales_amount FROM ci_core.fact_sales_daily;   -- permission denied for table
```

Every membership is granted `WITH INHERIT FALSE`. That is load-bearing: an
inheriting member holds the role's privileges with no `SET ROLE` at all, so `app`
would hold `SELECT` on both persona schemas at once and a request that skipped
`copilot_scope()` would answer an executive's question with money in it.
`scripts/bootstrap_roles.sql` asserts it, and revision `20260828_09` re-checks it
before granting anything.

### In a GUI client (DBeaver, DataGrip, …)

Put the `SET ROLE` block in the connection's **init / bootstrap SQL** so every new
session starts in the right role.

**Keep the client in auto-commit.** `SET ROLE` is transactional: in manual-commit
mode a single failed query plus a rollback silently reverts the session to `app`,
and every query afterwards runs as the owner — which reads everything, money
included, and looks like it worked. If a result surprises you, run
`SELECT current_user;` before believing it.

`ci_analyst` is also a login role, if you prefer separate credentials over
`SET ROLE`. It is created by `bootstrap_roles.sql` but stays `NOLOGIN` until you
give it a password (`CI_ANALYST_PASSWORD` in `.env`).

See `SETUP.md` for the full provisioning runbook.

## Metadata sourcing (glossary, enums, lookups)

All business metadata is served from Postgres at runtime. The CSVs under `data/`
are only offline **seed input** for the loaders.

- **Glossary / enums** — the `copilot_glossary` table is the runtime source of
  truth. `GlossaryService` reads it and, only if it is missing or empty, falls
  back to `data/v3_column_glossary.csv` (logged) so a fresh deploy still boots.
  Control with `GLOSSARY_SOURCE` (`postgres` default, or `csv`).
- **Store / product values** — the `copilot_lookup_value` table backs entity
  resolution. Every caller sees the same dictionary: with no OpCo or category
  scoping there are no out-of-scope entities, so an unresolved name is a typo and
  the right response is a clarification, not a refusal. No CSV is read at runtime.

### Seeding

Run after `alembic upgrade head` and whenever a seed CSV changes:

```bash
python -m app.scripts.load_reference_data      # product/store CSVs -> dims + lookup catalog
python -m app.scripts.load_glossary            # v3_column_glossary.csv -> copilot_glossary
python -m app.scripts.load_metric_definitions  # v3_metric_definition_seed.csv -> metric_definition
```

Under Docker Compose the `agent-api-migrate` service runs the migrations and all
three loaders automatically before the API starts.

### Mock fact data

Reference data alone leaves every fact table empty. To get something to query:

```bash
docker compose --profile seed run --rm mock-data
# or: python scripts/generate_mock_data.py --months 6 --customers 5000
```

Deterministic (seeded RNG) and shaped so the questions the copilot exists to
answer have recognisably correct answers — Evening outsells other dayparts,
membership penetration sits near 70%, and cross-OpCo overlap is non-trivial so an
overlap question does not return a zero that belongs to the generator rather than
the business. It `TRUNCATE`s the fact tables first, so re-running replaces rather
than merges.

The scripts are baked into the images, so rebuild after editing one:

```bash
docker compose build mock-data agent-api-migrate
```

## Currency

Every money column in `ci_core` is denominated in one currency — there is no per-row
currency column and no FX table — so the unit is a deployment constant, not a
property of a result row:

| setting | default | read by |
|---|---|---|
| `CURRENCY_CODE` | `MYR` | `ChartSpec.currency`, for a frontend to format with its own locale rules |
| `CURRENCY_SYMBOL` | `RM` | the narrative answer, which an LLM writes |

Left alone an LLM writes `$`, because most of its training data does. That is the
confident-zero failure in another costume: a wrong unit on a right number reads
exactly like a right answer. So the symbol is stated as a rule in every prompt that
produces user-facing prose, and applied directly by the deterministic answer paths,
which run when the LLM is skipped and no prompt rule can reach them.

**Which result column is money is derived from the SQL, not from its name.** Aliases
are written by the planner, so `total_sales`, `revenue`, `amt` and `sales_rm` are all
plausible names for one sum, and `count` appears inside `discount_amount`. Instead,
[app/service/currency.py](app/service/currency.py) walks the sqlglot AST and marks an
output column as money when its expression reaches a declared currency column,
propagated through CTEs and subqueries. Two consequences worth knowing:

- `gross_sales_amount / transaction_count` is money — an average basket.
- `gross_sales_amount / SUM(gross_sales_amount) OVER ()` is **not** — a ratio of money
  to money is a share, and a symbol on it would be wrong.

The currency columns themselves are derived from `MONEY_COLUMNS` in
[app/db/v3_ddl.py](app/db/v3_ddl.py), which already declares what an EXEC may not
see. That set is a superset of "is an amount": a sales rank is derived from revenue,
so an EXEC does not get it, but formatting a rank as RM would be nonsense. The exceptions are
declared as `NON_CURRENCY_MONEY_COLUMNS` rather than as a second list of money
columns, so the two cannot drift — a new `MONEY_COLUMNS` entry that is neither fails
`tests/test_currency.py`.

## Feature flags

- `GLOSSARY_SOURCE` — `postgres` (default) or `csv`.

### Notes

- Alembic supports both offline and online migration modes.
- `alembic/env.py` uses an async SQLAlchemy engine via `async_engine_from_config`.
- The repo stores migration revisions under `alembic/versions/`.

## Running the app locally

Use the FastAPI dev runner:

```bash
uv run fastapi dev
```

Or use Docker Compose if configured in your environment.
