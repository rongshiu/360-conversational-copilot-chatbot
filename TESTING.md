# Testing

Three levels, cheapest first. Each catches a class the others cannot.

## 1. Unit tests — no database

```bash
uv run pytest tests/ -q
```

Fast, and safe to run anywhere. Everything DB-dependent **skips** rather than fails,
so a green run here does *not* mean the schema and data are right — see level 2.

Covers the deterministic logic: entity matching and scoring, the SQL validator's
safety rules, persona isolation, period handling, row-cap disclosure.
Almost every test names the bug it exists to prevent, so a failure tells you which
behaviour regressed rather than only which assertion broke.

## 2. Schema and seed data — needs a live database

The in-container hostname is not resolvable from your machine, so point the tests at
the published port:

```bash
POSTGRES_URI=postgresql://app:app@localhost:5432/customer_intelligence \
  uv run pytest tests/ -q
```

That switches on two files which otherwise skip:

| file | what it guards |
|---|---|
| `tests/test_schema_drift.py` | the generated ORM still matches the live schema |
| `tests/test_seed_data.py` | the seeded data can actually answer the questions |

`test_seed_data.py` is the one that matters after any change to the loaders or the
generator. It asserts, among other things:

- **no table the planner can query is empty** — an empty advertised view answers `0`
  for a reason no user could guess, which is indistinguishable from a real finding
- every level-1 division reaches `agg_sales_daily`, so a category grant is answerable
- every store and every leaf category has sales
- `fact_sales_daily` carries the full `l1..l4` chain and matches `dim_product_category`
- the rollups reconcile with the atomic fact
- retail-to-retail *and* credit overlap both exist, so cross-OpCo questions are real
- `dim_date` covers the whole sales period, so a join to it cannot drop rows

It **skips** (rather than fails) when the fact tables are empty, and tells you to
seed.

## 3. End to end — needs the API and an LLM

```bash
curl -s localhost:8080/healthz

curl -s localhost:8080/v1/copilot/ask \
  -H 'Content-Type: application/json' -H 'x-user-id: edward' \
  -d '{"query":"which daypart performed best between 3 and 22 june 2026?",
       "thread_id":null,
       "permission":{"principal_id":"edward","role_level":"HOD"}}'
```

The answers below are stable for the seeded data, so they double as a smoke test.
Run them across roles, because the interesting failures are permission-shaped.

| question | permission | expected |
|---|---|---|
| sales performance and membership penetration, 3–22 June 2026 | NORTHCO / HOD | `analytics`, penetration ≈ 70% |
| which daypart performed best, 3–22 June 2026 | NORTHCO / HOD | `analytics`, **Evening** |
| same, but revenue-free | NORTHCO / **EXEC** | `analytics` in **units**, no money columns |
| how many customers buys hardline in 2026 | NORTHCO / EXEC | `analytics` |
| customers in Elite, Premium, Growth, Mass, At Risk, June 2026 | N360 / HOD | `analytics`, grouped by segment |
| how many NorthCo customers are also NorthCo Credit customers, June 2026 | NORTHCO / HOD | `analytics` — the sanctioned overlap |
| how many customers visited NorthCo Mart, June 2026 | NORTHCO / HOD | **`out_of_scope`** |
| how many Retail customers in Jan 2026 | NorthCo_Credit / HOD | **`out_of_scope`** (alias resolves to another OpCo) |
| customers with NorthCo Credit but **not** NorthCo Mart, Q1 2026 | NorthCo_Mart / HOD | **`unsupported`** — empty by definition |
| who bought item abc yesterday | any | **`out_of_scope`** — identity |
| how many customers all time | any | **`clarify`** — asks for a period |
| transactions and sales in June 2026 | **N360** / HOD + any category grant | same totals as N360 with no grant; the grant is ignored |

Every monetary figure must read `RM 21,034,575.95` — never `$`, and never on a count.
Money charts must carry `value_format: "currency"` and `currency: "MYR"`; count charts
must carry neither. Both directions are worth checking, since a symbol on a
transaction count is the same class of error as a missing one on revenue:

| question | expected |
|---|---|
| transactions and sales in June 2026 | `325,994 transactions` bare, `RM 21,034,575.95` |
| sales by product division, June 2026 | `bar_chart`, `value_format: currency`, `currency: MYR` |
| transactions by daypart, June 2026 | `bar_chart`, `value_format: count`, `currency: null` |
| units sold by daypart (EXEC) | `count`, no money anywhere in the answer |

### Category questions

There is no category grant any more, and no OpCo grant either — every caller sees
every OpCo and every product category. What used to be an authorization question
("is this category in your grant?") is now purely a resolution question ("which
category do you mean?"), so the interesting cases changed shape:

| ask | expected |
|---|---|
| "sales for HARD in June 2026" | answered; resolves to the level-1 node |
| "sales for OTHERS" | **clarification**, not a refusal — `OTHERS` is 63 nodes, so the copilot lists candidates and asks |
| "sales for BEAUTY" | clarification; `BEAUTY` is 3 nodes across levels |
| "beauty products at Inglegate Juniperford" | answered, with both entities resolved in `lookup_matches` |

An unresolved name is now a **typo, not a permission problem**. If any category
question comes back as `out_of_scope`, that is a regression: the only refusals left
are `customer_identity` and `role_money_withheld`.

To see the catalogue:

```sql
SET ROLE ci_analyst;   -- or ci_loader
SELECT category_key, category_level, category_path_text
FROM ci_core.dim_product_category
WHERE opco_code = 'NORTHCO' AND category_level <= 2
ORDER BY category_path_text;
```

#### Category depth still decides which table answers

This outlived the grants, for a different reason. `agg_sales_daily` is aggregated to
level 2, so it has no `category_l3_key`/`l4_key`: a level-3 question cannot be
answered there, and a level-2 rollup row aggregates siblings the question did not
ask about. `CATEGORY_DEPTH` therefore removes any table shallower than the category
the question names, and the planner never gets the chance to pick one that would
answer about the wrong thing.

| question names | must be answered from |
|---|---|
| L1 `HARD` | `v_sales_summary_daily` (or deeper) |
| L2 `HARD > DIY` | `v_sales_summary_daily` (or deeper) |
| L3 `… > HOME REPAIR & IMPROVEMENT` | `v_sales_daily` |
| L4 `… > DOORS ACCESSORIES` | `v_sales_daily` |

Verify by checking which view the returned SQL names. A level-4 question landing on
`v_sales_summary_daily` is a routing regression, and the number it returns is about
a coarser category than the one asked for.

Note the two source tables are generated independently — the fact is a deliberate
sample, the agg is not — so numbers are comparable *within* a column, never across.

### Persona isolation

The HOD/EXEC split is enforced by PostgreSQL, so it is testable without the API:

```sql
BEGIN;
SET LOCAL ROLE ci_copilot_exec;
SELECT count(*) FROM ci_hod.v_sales_daily;      -- must ERROR: permission denied
SELECT count(*) FROM ci_core.fact_sales_daily;  -- must ERROR: permission denied
ROLLBACK;
```

Through the API, ask the same money question as both roles:

| `role_level` | ask | expected |
|---|---|---|
| `HOD` | "sales in June 2026" | revenue in `RM`, `value_format: currency` |
| `EXEC` | "sales in June 2026" | transaction counts, plus a `role_money_withheld` policy with `effect: substituted` |
| `EXEC` | "average basket value in June 2026" | refused, `effect: denied` — units per basket is a different quantity, not an honest substitute |

### Streaming (`/v1/copilot/ask/stream`)

```bash
curl -sN localhost:8080/v1/copilot/ask/stream \
  -H 'Content-Type: application/json' -H 'x-user-id: edward' \
  -d '{"query":"show me sales by product division for june 2026","thread_id":null,
       "permission":{"principal_id":"edward","role_level":"HOD"}}'
```

`-N` matters: without it curl buffers and every event appears at once, which looks
like streaming is broken when it is not.

Expected frame order: `status`* → `delta`* → `meta` → `final` → `done`. A `: keepalive`
comment appears wherever a stage runs longer than 10s. Analytics answers stream real
token deltas; every other type arrives as one delta, so a client renders both the
same way.

Three things worth testing deliberately, because all three were wrong at some point:

| what | how | expected |
|---|---|---|
| a bad permission block | `role_level: "CFO"`, or a retired `opco_codes` field | **HTTP 422** naming the field — not `200` + an error event |
| dead air | watch timestamps during planning | a `: keepalive` comment, never silence past ~10s |
| client hangs up | `--max-time 3`, then read the server log | work stops within one in-flight LLM call; **no** `requires 'ci_copilot_hod'` role-assertion lines |

That last one is the subtle one. A disconnect arrives as `GeneratorExit`, which is a
`BaseException` and so invisible to `except Exception` — the graph used to keep
running for 13+ seconds, and its SQL then failed the role assertion because the
request scope had been torn down underneath it. The role guard caught it (so nothing
leaked), but the run burned every retry on behalf of a client that had gone.

One artifact remains on the disconnect path: SQLAlchemy logs
`Exception terminating connection ... CancelledError` once, because Starlette cancels
the request scope while the pool is closing the connection. The connection is
discarded rather than reused, so it is noise rather than a leak — verified by
repeated disconnects leaving no accumulation in `pg_stat_activity`.

### What "wrong" looks like

The failure mode to watch for is not an error. It is a **confident zero**: a real
number answering a question nobody asked. If a query returns `0` or `null`, check
which it is before believing it:

- **empty** — no matching rows; the answer must say "no records", never "zero", and never claim anything was withheld — nothing is
- **refused** — a privacy or role boundary; must be `out_of_scope`/`unsupported` with a policy notice, never `0`. Note there is no OpCo or category boundary left: a category question coming back refused is itself the bug
- **unseeded** — a gap in the mock data; that is what level 2 is for

## Reseeding

Scripts are baked into the images, so **rebuild first** or you keep running the old
code:

```bash
docker compose build agent-api agent-api-migrate mock-data

docker compose up -d                                  # migrations + reference data
docker compose --profile seed run --rm mock-data       # fact data
```

Reference data (`dim_opco`, `dim_product_category`, `dim_store`, `dim_product`,
`dim_date`, the lookup catalog) is owned by `app.scripts.load_reference_data` and
reloaded on every `docker compose up`. Fact data is owned by
`scripts/generate_mock_data.py`.

That ownership split matters: the loader truncates `dim_product_category` **CASCADE**,
and `dim_product` has a foreign key to it. While the SKU dimension was generated by
the fact script, every `docker compose up` silently deleted it and left
`agg_sales_sku_monthly` pointing at 63,192 rows that no longer existed. A dimension
belongs to the loader that owns its parent.

Both are deterministic: same seed, same rows. A changed answer is a code change, not
a data change.

## Querying the data yourself

`app` owns the tables, so a plain `SELECT` as `app` reads **everything** — which
means ad-hoc queries as `app` tell you nothing about what a caller can see. See the
"Querying the data directly" section of `README.md` — in short, `SET ROLE
ci_analyst` to browse everything read-only, or `SET ROLE ci_copilot_hod` /
`ci_copilot_exec` plus the three GUCs to reproduce exactly what one caller sees.
