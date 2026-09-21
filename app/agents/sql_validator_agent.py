# app/agents/sql_validator_agent.py
"""Deterministic SQL safety and shape validation.

Rewritten for v3. The v2 validator assumed schema-qualified table names, joins on
`customer_id`, and a `snapshot_date` column -- none of which exist now. More
importantly it had no notion of an identity column, which is the rule that
actually matters here.

The identity rule is load-bearing, not defence in depth. Testing confirmed that
`customer_key` IS selectable at the database level: COUNT(DISTINCT x) requires
SELECT privilege on x, so PostgreSQL column privileges cannot express "countable
but not projectable". This module is the only thing preventing
`SELECT customer_key FROM v_customer_opco_monthly`.

Order of checks is deliberate: cheap structural rejections first, so an obviously
bad query never reaches the glossary lookups.

Parsing is done by sqlglot, not by regex. The rules below are unchanged, but what
they ask questions OF is now an AST. Three consecutive production failures came
from hand-rolled parsing being wrong about VALID SQL:

    EXTRACT(YEAR FROM c.month_start_date)   read as a table reference, then
                                            rejected as "schema-qualified"
    FROM RankedSegmentRevenue WHERE rn = 1  read as alias `WHERE`, a reserved word
    FROM RankedSegmentRevenue r ... r.y     alias bound to a CTE treated as a real
                                            table, so a computed column "did not
                                            exist"

Each was individually fixable and each cost a user a working query. Window
functions, lateral joins, quoted identifiers and UNION were queued up behind them.

An AST is also strictly SAFER for the rule that matters most. The identity check
used to strip legal `customer_key` uses with regexes and scan the residue; now the
usage is classified by its position in the tree, so `customer_key` projected from
inside a CTE is seen directly rather than inferred from what is left over.
"""
from __future__ import annotations

import re
from functools import lru_cache

import sqlglot
from sqlglot import exp
from pydantic import BaseModel

from app.core.logging import Logger
from app.service.glossary_service import get_glossary_service
from app.service.table_registry import (
    ALLOWED_VIEWS,
    IDENTITY_COLUMNS,
    JOINABLE_DIMENSIONS,
    CUSTOMER_GRAIN_VIEWS,
    NEVER_AGGREGATE_COLUMNS,
    PER_CUSTOMER_CONSTANT_COLUMNS,
    PERIOD_COLUMN,
    PERIOD_REQUIRED_VIEWS,
    is_forbidden_table,
    normalize_table_name,
)

logger = Logger.get_logger(__name__)

# Aliases the planner reaches for that PostgreSQL rejects as bare identifiers.
# `dim_opco AS do` is the one seen in practice -- DO is reserved, so the query dies
# with `syntax error at or near "do"`, which the planner cannot diagnose from the
# message alone. Rejecting it here produces an actionable retry instead.
RESERVED_ALIASES: frozenset[str] = frozenset({
    "do", "as", "is", "in", "on", "or", "and", "not", "all", "any", "to", "from",
    "where", "select", "group", "order", "by", "join", "left", "right", "full",
    "inner", "outer", "union", "with", "case", "when", "then", "else", "end",
    "table", "user", "using", "into", "limit", "offset", "having", "distinct",
    "between", "like", "null", "true", "false", "asc", "desc", "over", "window",
    "cast", "check", "default", "column", "current_user", "session_user",
})


class SqlValidationResult(BaseModel):
    is_valid: bool
    normalized_sql: str = ""
    feedback: str = ""


# ---------------------------------------------------------------------------
# Structural patterns
# ---------------------------------------------------------------------------

_MUTATING = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|"
    r"vacuum|copy|call|reindex|cluster|listen|notify|"
    r"security\s+label|comment\s+on|refresh\s+materialized)\b",
    re.IGNORECASE,
)

# A generated query must not touch the session. set_config would let it rewrite
# the very GUCs the RLS policies read -- i.e. widen its own access scope
# inglegate-query. RESET/SET ROLE would drop out of the scoped role entirely.
_SESSION_TAMPERING = re.compile(
    r"\b(set\s+role|reset\s+role|set\s+local|set\s+session|reset\s+all)\b"
    r"|\b(set_config|current_setting|pg_read_file|pg_read_binary_file|pg_ls_dir|"
    r"dblink|dblink_exec|pg_stat_file|lo_import|lo_export|pg_sleep)\s*\(",
    re.IGNORECASE,
)

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"--[^\n]*")


def _strip_sql(sql: str) -> str:
    """Remove comments and collapse whitespace.

    Comments go first so a rule cannot be evaded by hiding a column reference in
    one, and so the identity check does not fire on a column merely mentioned in
    prose.
    """
    text = _COMMENT_BLOCK.sub(" ", sql or "")
    text = _COMMENT_LINE.sub(" ", text)
    return " ".join(text.split()).strip().rstrip(";").strip()


def parse_sql(sql: str) -> exp.Expression | None:
    """Parse to an AST, or None when the text is not valid SQL.

    None is a rejection, not a bypass: unparseable text must never reach the
    database, and a parser that cannot read the query cannot vouch for it either.
    """
    try:
        return sqlglot.parse_one(sql, dialect="postgres")
    except Exception:  # noqa: BLE001 -- ParseError, TokenError, RecursionError...
        return None


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE) if cte.alias_or_name}


def _tables(tree: exp.Expression) -> list[exp.Table]:
    """Every table reference, CTE references excluded.

    A CTE is a name defined by this query, so it is neither a real table to
    allow-list nor a source of columns the glossary could know about.
    """
    ctes = _cte_names(tree)
    return [tbl for tbl in tree.find_all(exp.Table) if tbl.name.lower() not in ctes]


def _extract_referenced_tables(tree: exp.Expression) -> set[str]:
    """Table names as written, so schema qualification stays visible."""
    return {
        f"{tbl.db}.{tbl.name}" if tbl.db else tbl.name
        for tbl in _tables(tree)
    }


def _alias_bindings(tree: exp.Expression) -> dict[str, str]:
    """alias (and bare table name) -> table name, for real tables only.

    CTE aliases are deliberately absent: their columns are computed by the query,
    so there is nothing to check them against.
    """
    bindings: dict[str, str] = {}
    for tbl in _tables(tree):
        name = tbl.name.lower()
        bindings[name] = name
        if tbl.alias:
            bindings[tbl.alias.lower()] = name
    return bindings


def _projected_columns(tree: exp.Expression) -> list[exp.Column]:
    """Columns in a SELECT list, GROUP BY or ORDER BY -- anywhere a value is
    returned or used to shape the result, as opposed to filtered on."""
    found: list[exp.Column] = []
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            found.extend(projection.find_all(exp.Column))
        for clause in (select.args.get("group"), select.args.get("order")):
            if clause is not None:
                found.extend(clause.find_all(exp.Column))
    return found


def _where_columns(tree: exp.Expression) -> list[exp.Column]:
    """Columns used in a WHERE clause."""
    found: list[exp.Column] = []
    for where in tree.find_all(exp.Where):
        found.extend(where.find_all(exp.Column))
    return found


# ---------------------------------------------------------------------------
# Identity column rule
# ---------------------------------------------------------------------------

def _is_inside_count_distinct(column: exp.Column) -> bool:
    """COUNT(DISTINCT customer_key) -- the one aggregate that may touch identity."""
    node = column.parent
    while node is not None:
        if isinstance(node, exp.Count):
            return isinstance(node.this, exp.Distinct) or bool(node.args.get("distinct"))
        # A clause boundary means the column is being used, not merely counted.
        if isinstance(node, (exp.Select, exp.Where, exp.Join, exp.Group, exp.Order)):
            return False
        node = node.parent
    return False


def _is_join_equality(column: exp.Column) -> bool:
    """`ON a.customer_key = b.customer_key` -- required by the overlap query.

    Both sides must be the same identity column, so this cannot smuggle the value
    into a comparison against a literal.
    """
    parent = column.parent
    if not isinstance(parent, exp.EQ):
        return False
    left, right = parent.this, parent.expression
    if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
        return False
    return left.name.lower() == right.name.lower() == column.name.lower()


def _outermost_selects(tree: exp.Expression) -> set[int]:
    """The SELECTs whose projections the user actually receives.

    A UNION returns both branches, so both count. Everything else -- CTEs, derived
    tables, scalar subqueries -- feeds a computation rather than the result set.
    """
    if isinstance(tree, exp.Union):
        return {
            id(side)
            for side in (tree.this, tree.expression)
            if isinstance(side, exp.Select)
        }
    if isinstance(tree, exp.Select):
        return {id(tree)}
    inner = tree.find(exp.Select)
    return {id(inner)} if inner is not None else set()


def _enclosing_select(node: exp.Expression) -> exp.Select | None:
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        parent = parent.parent
    return parent if isinstance(parent, exp.Select) else None


def _in_clause(node: exp.Expression, select: exp.Select, key: str) -> bool:
    clause = select.args.get(key)
    return clause is not None and any(node is c for c in clause.find_all(exp.Column))


def _in_projection(node: exp.Expression, select: exp.Select) -> bool:
    return any(
        node is c for projection in select.expressions for c in projection.find_all(exp.Column)
    )


def _validate_union_order_by(tree: exp.Expression) -> tuple[bool, str]:
    """After a UNION, ORDER BY may only name an output column.

    PostgreSQL resolves the ORDER BY of a set operation against the RESULT, not
    against either branch, so a table alias there refers to nothing:

        SELECT d.store_name ... FROM WangsaMajuData AS d
        UNION ALL
        SELECT d.store_name ... FROM BandarUtamaData AS d
        ORDER BY d.store_name        -- missing FROM-clause entry for table "d"

    That is valid-looking SQL, so it passed every rule here and failed in the
    database instead -- where the retry got the generic "referenced a table column
    incorrectly" advice, wrote the same thing again, and the question died after two
    attempts. Catching it here costs one comparison and returns feedback that names
    the fix.
    """
    if not isinstance(tree, exp.Union):
        return True, ""

    order = tree.args.get("order")
    if order is None:
        return True, ""

    qualified = sorted(
        {c.table for c in order.find_all(exp.Column) if c.table}
    )
    if not qualified:
        return True, ""

    return False, (
        "After UNION / UNION ALL, ORDER BY is resolved against the combined result, "
        f"so it cannot use a table alias ({', '.join(qualified)}). Order by the "
        "output column NAME alone -- `ORDER BY store_name`, not "
        "`ORDER BY d.store_name`."
    )


def _validate_identity_columns(tree: exp.Expression) -> tuple[bool, str]:
    """Identity may be counted, joined on, or used internally. Never returned.

    The rule is about what reaches the USER, so it is positional:

      refused   identity in the OUTERMOST projection or GROUP BY
      refused   any name aliased from an expression containing identity, when THAT
                name reaches the outermost projection or GROUP BY
      refused   identity inside an aggregate other than COUNT(DISTINCT)
      allowed   everything else -- CTEs, derived tables, WHERE, JOIN ... ON

    Two earlier strictnesses were dropped because they cost real questions and
    bought nothing.

    The first refused an inner projection unless it was backed by its own GROUP BY.
    `SELECT DISTINCT customer_key` and `SELECT customer_key ... GROUP BY
    customer_key` mean the same thing, and the rule took one and refused the other;
    worse, it refused the plain set a semi-join needs:

        WITH bank AS (
          SELECT customer_key FROM v_customer_opco_monthly
          WHERE opco_code = 'NORTHCO_BANK' AND is_active_in_opco
        )
        SELECT bank_user, SUM(spend) FROM (
          SELECT CASE WHEN b.customer_key IS NULL THEN 'no' ELSE 'yes' END AS bank_user,
                 r.total_revenue AS spend
          FROM v_customer_opco_monthly r
          LEFT JOIN bank b ON b.customer_key = r.customer_key
          WHERE r.opco_code IN ('NORTHCO','NORTHCO_MART')
        ) x GROUP BY bank_user

    which is the shape of every "compare X between customers who did A and those
    who did not" question -- the ecosystem questions this schema exists for. "How
    does retail spend compare between members who use NorthCo Bank and those who do
    not" came back as "the data model does not support" for a query the data model
    supports perfectly well.

    The second was refusing identity in the outermost WHERE. A predicate returns
    nothing; only the projection and the grouping do.

    What replaces them is tighter, not looser. Aliases are now followed: any name
    bound to an expression CONTAINING identity is treated as identity wherever that
    name goes, so `SELECT k FROM (SELECT max(customer_key) AS k ...)` and
    `SELECT k FROM (SELECT substr(customer_key,1,5) AS k ...)` are both refused --
    neither was, before. Matching on the name makes it transitive through any depth
    of nesting. And `SELECT *` is refused outright elsewhere, so an outermost
    projection is always explicit and always seen here.
    """
    identity = {c.lower() for c in IDENTITY_COLUMNS}
    outermost = _outermost_selects(tree)

    # Names bound to an expression that RETURNS identity data. Mentioning identity
    # is not enough: `CASE WHEN b.customer_key IS NULL THEN 'no' ELSE 'yes' END`
    # reads an identity and returns a flag, which is the whole point of a semi-join
    # and gives nothing away. `substr(customer_key, 1, 6)` returns part of the
    # identifier itself and does. See _returns_identity.
    derived: set[str] = {
        alias.alias.lower()
        for alias in tree.find_all(exp.Alias)
        if _returns_identity(alias.this, identity)
    }

    for column in tree.find_all(exp.Column):
        name = column.name.lower()
        is_identity = name in identity

        if not is_identity and name not in derived:
            continue

        # No aggregate but COUNT(DISTINCT) may touch an identity value: MAX, MIN,
        # STRING_AGG and friends compute an identifier out of a set of them.
        if is_identity and _is_inside_disallowed_aggregate(column):
            return False, _identity_feedback(name)

        if is_identity and (_is_inside_count_distinct(column) or _is_join_equality(column)):
            continue

        select = _enclosing_select(column)
        if select is None:
            return False, _identity_feedback(name)

        if id(select) not in outermost:
            continue

        # Outermost. What is refused is an expression that RETURNS the identity,
        # in either the projection or the grouping -- not one that merely mentions
        # it. `GROUP BY c.customer_key` is a customer-level result set and stays
        # refused; `GROUP BY CASE WHEN b.customer_key IS NULL THEN 'no' ELSE 'yes'
        # END` is two groups, and is the shape every "A-doers versus everyone else"
        # question takes. The same test already governs aliases, so applying it here
        # makes this one rule rather than three.
        group = select.args.get("group")
        clauses = list(select.expressions)
        if group is not None:
            clauses.extend(group.expressions)

        for clause in clauses:
            if not any(c is column for c in clause.find_all(exp.Column)):
                continue
            if _returns_identity(clause, identity | derived):
                return False, _identity_feedback(name, derived=not is_identity)

    return True, ""


# Nodes whose result is a boolean, whatever they are computed from. An identity
# inside one of these is being TESTED, not returned.
_PREDICATE_NODES = (
    exp.Is, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
    exp.In, exp.Like, exp.ILike, exp.Not, exp.And, exp.Or, exp.Between, exp.Exists,
)


def _returns_identity(node: exp.Expression, identity: set[str]) -> bool:
    """Whether evaluating this expression yields identity data.

    The question is what comes OUT, not what goes in. A comparison over an identity
    yields a boolean; a CASE yields whichever branch it selects, and its condition
    is not one of them; COUNT yields a number. Anything else that touches identity
    is assumed to carry it through, because substr, cast, concat and their like do.
    """
    if isinstance(node, exp.Column):
        return node.name.lower() in identity
    if isinstance(node, _PREDICATE_NODES):
        return False
    if isinstance(node, exp.Count):
        return False
    if isinstance(node, exp.Case):
        branches = [entry.args.get("true") for entry in node.args.get("ifs") or []]
        branches.append(node.args.get("default"))
        return any(b is not None and _returns_identity(b, identity) for b in branches)
    if isinstance(node, exp.Alias):
        return _returns_identity(node.this, identity)

    return any(
        _returns_identity(child, identity)
        for child in node.iter_expressions()
    )


def _is_inside_disallowed_aggregate(node: exp.Expression) -> bool:
    """Inside an aggregate that is not COUNT(DISTINCT identity).

    Defers to _is_inside_count_distinct for the allowed case rather than testing
    for DISTINCT here: sqlglot models `COUNT(DISTINCT x)` as Count(Distinct(x)), so
    the flag is a wrapper node and not an argument of the Count.
    """
    if _is_inside_count_distinct(node):
        return False

    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        if isinstance(parent, exp.AggFunc):
            return True
        parent = parent.parent
    return False


def _identity_feedback(name: str, derived: bool = False) -> str:
    if derived:
        identity = ", ".join(f"`{c}`" for c in IDENTITY_COLUMNS)
        return (
            f"`{name}` is derived from an identity column ({identity}), so returning "
            "it returns the identifier under another name. Aggregate to a count "
            "instead, or keep the identity inside a subquery and return only the "
            "aggregate."
        )
    return (
        f"`{name}` is an identity column. It may appear inside COUNT(DISTINCT "
        f"{name}), in a JOIN ... ON equality between two aliases, or as a GROUP BY "
        "key in a SUBQUERY whose outer query aggregates it away. It must never be "
        "returned: not in the final SELECT list, not grouped by at the top level, "
        "and never inside another aggregate such as MAX(). Aggregate to a count "
        "instead."
    )


# ---------------------------------------------------------------------------
# Customer grain
# ---------------------------------------------------------------------------

def _validate_customer_grain(tree: exp.Expression) -> tuple[bool, str]:
    """A row count is not a customer count on a view where a customer repeats.

    `SELECT COUNT(*) FROM v_customer_opco_monthly WHERE month_start_date = ...`
    parses, executes, returns a number, and that number is wrong: a customer active
    in two OpCos occupies two rows. Nothing else in the validator catches it,
    because the query never mentions `customer_key` and so the identity rule -- the
    one that makes every other customer answer safe -- never fires.

    Why this rule did not exist before. Row-level security scoped
    v_customer_opco_monthly to the caller's OpCos, so a single-OpCo caller saw
    exactly one row per customer per month and COUNT(*) was right for them. That
    was the common case, and it is the case that stopped being true when the row
    filter came off: every caller now sees all four OpCos.

    Any COUNT without DISTINCT is refused, rather than only COUNT(*). COUNT(1) and
    COUNT(some_column) count rows just as surely, and no business question on these
    views asks for a row count. COUNT(DISTINCT ...) of any column is fine --
    de-duplication is exactly what makes the answer independent of row multiplicity.

    The rule is per-SELECT, so the collapse-then-count pattern still passes:
    SELECT count(*) FROM (SELECT customer_key FROM v_customer_opco_monthly
    GROUP BY customer_key) x counts a derived table that is already one row per
    customer, not the view.
    """
    for select in tree.find_all(exp.Select):
        sources = {
            normalize_table_name(table.name)
            for table in select.find_all(exp.Table)
            if table.parent_select is select
        }
        hit = sources & CUSTOMER_GRAIN_VIEWS
        if not hit:
            continue

        for count in select.find_all(exp.Count):
            if count.parent_select is not select:
                continue
            if isinstance(count.this, exp.Distinct) or count.args.get("distinct"):
                continue
            view = sorted(hit)[0]
            return False, (
                f"COUNT without DISTINCT on `{view}` counts ROWS, not customers. "
                + (
                    "A customer occupies one row per OpCo per month here, so anyone "
                    "active in two OpCos is counted twice."
                    if view == "v_customer_opco_monthly"
                    else "A customer occupies one row per leaf category per store "
                    "here, so anyone who shopped two branches is counted twice."
                )
                + " Use COUNT(DISTINCT customer_key). If you genuinely need to count "
                "something else, count it distinctly too -- "
                "COUNT(DISTINCT store_id) and the like are fine."
            )

    return True, ""


# ---------------------------------------------------------------------------
# Non-additive measures
# ---------------------------------------------------------------------------

def _validate_aggregation_rules(tree: exp.Expression) -> tuple[bool, str]:
    """Reject SUM/AVG over measures that are not additive.

    From the AST, so `SUM(s.customer_count)` and `sum( DISTINCT customer_count )`
    are the same finding, and a column merely named in a comment or a string cannot
    trigger it.
    """
    never = {c.lower() for c in NEVER_AGGREGATE_COLUMNS}

    for node in tree.find_all(exp.Sum, exp.Avg):
        for column in node.find_all(exp.Column):
            name = column.name.lower()
            if name not in never:
                continue

            func = "SUM" if isinstance(node, exp.Sum) else "AVG"
            if name in PER_CUSTOMER_CONSTANT_COLUMNS:
                return False, (
                    f"{func}({name}) is wrong. `{name}` is a PER-CUSTOMER value "
                    "repeated on every row that customer occupies -- one row per "
                    "OpCo per month on v_customer_opco_monthly -- so aggregating it "
                    "across rows weights it by how many OpCos the customer shops "
                    "with. De-duplicate to one row per customer first: "
                    f"SELECT {func.lower()}({name}) FROM (SELECT DISTINCT "
                    f"customer_key, {name} FROM <view> WHERE ...) x"
                )
            if name == "customer_count":
                return False, (
                    f"{func}({name}) is wrong. `customer_count` is distinct "
                    "customers AT ONE ROW GRAIN ONLY -- summing it across days or "
                    "categories double-counts anyone appearing in more than one row, "
                    "and averaging it is meaningless. For a distinct customer count "
                    "over a period use COUNT(DISTINCT customer_key) on "
                    "v_customer_opco_monthly or v_customer_category_monthly."
                )
            return False, (
                f"{func}({name}) is not valid. `{name}` is a precomputed rank; "
                "filter or order by it instead of aggregating it."
            )
    return True, ""


def _validate_store_name_filters(tree: exp.Expression) -> tuple[bool, str]:
    """Require branch filters to be canonical keys from lookup resolution."""
    if not any(c.name.lower() == "store_name" for c in _where_columns(tree)):
        return True, ""

    return False, (
        "`store_name` cannot be used as a WHERE filter. Store and branch names must "
        "come from resolved lookup context, then be filtered internally by `store_id` "
        "together with `opco_code`. If no store was resolved, ask the user to choose "
        "from named branch options or provide the branch name; do not ask for IDs."
    )


def _validate_category_name_filters(tree: exp.Expression) -> tuple[bool, str]:
    """Require product filters to be resolved category keys, same as stores.

    Without this the planner filled an unresolved product word in itself: asked
    "how many customers buys hardline in 2026" -- where "hardline" is the user's
    word for the HARD division and so resolves to nothing -- it emitted
    `LOWER(TRIM(pc.category_name)) = LOWER(TRIM('Hardline'))`. No such row exists,
    the query returned nothing, and the empty result was on its way to being
    narrated as "no customers buy hardline". A guessed literal that matches no row
    is indistinguishable from a real absence, which is why the resolver exists.

    Rejecting it sends the planner back with instructions to ask instead, so an
    unrecognised product name becomes a clarification rather than a false zero.
    """
    if not any(c.name.lower() == "category_name" for c in _where_columns(tree)):
        return True, ""

    return False, (
        "`category_name` cannot be used as a WHERE filter. Product and category "
        "names must come from resolved lookup context, then be filtered by "
        "`category_key` or the matching `category_lN_key`. If no category was "
        "resolved, do not guess a name -- return needs_clarification and ask the "
        "user which category they mean, using business labels rather than keys."
    )


# ---------------------------------------------------------------------------
# Table allow-list
# ---------------------------------------------------------------------------

def _validate_tables(referenced: set[str]) -> tuple[bool, str]:
    if not referenced:
        return False, (
            "No table reference found. Query one of the allowed views, unqualified: "
            + ", ".join(ALLOWED_VIEWS)
        )

    allowed = {v.lower() for v in ALLOWED_VIEWS} | {d.lower() for d in JOINABLE_DIMENSIONS}

    for raw in sorted(referenced):
        short = normalize_table_name(raw)

        # Schema qualification defeats the persona-view mechanism: naming
        # ci_hod.v_sales_daily would pin an executive to the HOD view, and naming
        # a ci_core table bypasses the view layer altogether.
        if "." in raw:
            return False, (
                f"`{raw}` is schema-qualified. Use the bare name `{short}` and let the "
                "database resolve it -- qualifying it bypasses the role-based view "
                "selection."
            )

        if is_forbidden_table(short):
            return False, (
                f"`{short}` is not queryable directly. Use its view instead, one of: "
                + ", ".join(ALLOWED_VIEWS)
            )

        if short.lower() not in allowed:
            return False, (
                f"`{short}` is not an available table. Allowed views: "
                + ", ".join(ALLOWED_VIEWS)
                + ". Joinable dimensions: "
                + ", ".join(JOINABLE_DIMENSIONS)
                + "."
            )

    return True, ""


def _validate_aliases(tree: exp.Expression) -> tuple[bool, str]:
    """Reject table aliases PostgreSQL rejects as bare identifiers.

    From the AST, so `FROM RankedSegmentRevenue WHERE rn = 1` cannot be misread as
    an alias named WHERE -- a parser knows a clause keyword is not an alias, which
    is exactly what the regex version could not know. `_NOT_AN_ALIAS`, the keyword
    list that existed only to compensate, is gone.
    """
    for tbl in tree.find_all(exp.Table):
        alias = (tbl.alias or "").lower()
        if alias and alias in RESERVED_ALIASES:
            return False, (
                f"`{alias}` is a reserved word and cannot be used as a table alias -- "
                f"PostgreSQL fails with a syntax error. Use a distinct alias such as "
                f"`{alias[0]}1` or a short meaningful name (o for dim_opco, "
                "s for dim_store, c for a category table)."
            )
    return True, ""


def _validate_period_filter(
    tree: exp.Expression, referenced: set[str]
) -> tuple[bool, str]:
    """Every v3 view is period-grained, so an unbounded scan is always a mistake.

    v2 could fall back to a customer snapshot table with no time dimension. No such
    table exists now, so a question with no period scans all history.
    """
    shorts = {normalize_table_name(r) for r in referenced}
    period_views = shorts & set(PERIOD_REQUIRED_VIEWS)
    if not period_views:
        return True, ""

    expected = {PERIOD_COLUMN[v] for v in period_views if v in PERIOD_COLUMN}
    if not expected:
        return True, ""

    # A predicate, BETWEEN, IN, or a date function applied to the column counts as
    # bounded. Merely selecting it does not.
    bounding = (
        exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
        exp.Between, exp.In, exp.DateTrunc, exp.Extract, exp.Anonymous, exp.Func,
    )
    for column in tree.find_all(exp.Column):
        if column.name.lower() not in expected:
            continue
        node = column.parent
        while node is not None and not isinstance(node, (exp.Select, exp.Join)):
            if isinstance(node, bounding):
                return True, ""
            node = node.parent

    return False, (
        "The query has no time-period filter. Every serving table is period-grained, "
        f"so add a predicate on {', '.join(sorted(expected))} -- for example BETWEEN "
        "two dates -- rather than scanning all history."
    )


def _validate_columns_exist(
    tree: exp.Expression, referenced: set[str]
) -> tuple[bool, str]:
    """Reject qualified columns that do not exist on the table they qualify.

    Per alias, not against the union of every referenced table. The union version
    accepted `dim_product_category AS pc ... ON cc.category_key = pc.category_l2_key`
    because `category_l2_key` is real -- on the OTHER table in the query. Postgres
    then failed at execution with UndefinedColumn, which the planner cannot act on.

    The naming split behind that is worth knowing: dim_product_category exposes its
    ancestor chain as `l1_key..l4_key`, while every serving view exposes
    `category_l1_key..category_l4_key`.

    Columns qualified by a CTE alias are skipped -- they are computed by the query,
    so no table in the glossary has them and none ever will. `_alias_bindings`
    contains real tables only, which is what makes that automatic.
    """
    glossary = get_glossary_service()
    shorts = [normalize_table_name(r) for r in referenced]
    bindings = _alias_bindings(tree)

    columns_by_table = {
        table: {c.lower() for c in glossary.get_columns_for_table(table)}
        for table in set(bindings.values())
    }

    known: set[str] = set()
    for short in shorts:
        known |= {c.lower() for c in glossary.get_columns_for_table(short)}

    if not known:
        # Nothing loaded for these tables yet (fresh deploy, loader not run). Skip
        # rather than reject every query.
        return True, ""

    for column in tree.find_all(exp.Column):
        alias = (column.table or "").lower()
        if not alias:
            # Unqualified: could be a CTE output, an alias defined in this SELECT,
            # or a bare column. Not decidable here, and the database will say so.
            continue

        name = column.name.lower()
        table = bindings.get(alias)
        table_columns = columns_by_table.get(table or "")

        if table_columns:
            if name in table_columns:
                continue
            similar = glossary.find_similar_columns(table, name, limit=4)
            hint = f" Did you mean: {', '.join(similar)}?" if similar else ""
            elsewhere = (
                " That column exists on another table in this query, so check which "
                "alias you are qualifying it with."
                if name in known
                else ""
            )
            return False, (
                f"Column `{name}` does not exist on `{table}` (alias `{alias}`)."
                f"{hint}{elsewhere} Use only columns from the provided schema context."
            )

        # Alias not bound to a real table: a CTE, a subquery, or a shape this does
        # not model. The database is the authority on those.
        continue

    return True, ""





# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def validate_sql(sql: str | None) -> SqlValidationResult:
    """Validate one generated statement.

    Three arguments are gone: overlap_opcos, granted_opcos and is_group_user. They
    fed two rules that existed because a predicate naming an OpCo outside the
    caller's grant matched no row -- not for want of data, but because row-level
    security had already restricted the query -- and that zero was then narrated as
    a fact about the business ("no customer activity was captured for this OpCo").

    Every OpCo is now visible to every caller, so such a query returns the true
    answer and there is nothing to intercept. The rules are removed rather than
    left in place always passing, because a validator rule that cannot fire is a
    rule nobody notices has stopped protecting anything.
    """
    if not sql or not sql.strip():
        return SqlValidationResult(is_valid=False, feedback="Planner returned empty SQL.")

    normalized = _strip_sql(sql)
    low = normalized.lower()

    def reject(feedback: str) -> SqlValidationResult:
        return SqlValidationResult(
            is_valid=False, normalized_sql=normalized, feedback=feedback
        )

    # --- structural -------------------------------------------------------
    if ";" in normalized:
        return reject("Only one SQL statement is allowed.")

    if not (low.startswith("select") or low.startswith("with")):
        return reject("Only SELECT/WITH queries are allowed.")

    if _MUTATING.search(low):
        return reject("Mutating SQL is not allowed.")

    if _SESSION_TAMPERING.search(low):
        return reject(
            "Functions or statements that read or change session settings, or reach "
            "outside the database, are not allowed -- they could alter the access scope "
            "this query runs under."
        )

    # --- parse once; every rule below reads the tree ----------------------
    #
    # The text checks above stay as text checks on purpose. They are a cheap guard
    # that runs BEFORE the parser, so a statement the parser might normalise or
    # tolerate cannot reach the database on the strength of the parser's opinion.
    tree = parse_sql(normalized)
    if tree is None:
        return reject(
            "That SQL could not be parsed. Rewrite it as a single valid "
            "PostgreSQL SELECT (or WITH ... SELECT) statement."
        )

    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery)):
        return reject("Only SELECT/WITH queries are allowed.")

    # A bare star, as opposed to COUNT(*).
    if any(
        not isinstance(star.parent, (exp.Count, exp.Anonymous, exp.Func))
        for star in tree.find_all(exp.Star)
    ):
        return reject("SELECT * is not allowed. Select only the columns you need.")

    union_ok, union_feedback = _validate_union_order_by(tree)
    if not union_ok:
        return reject(union_feedback)

    # --- the rules that carry real weight ---------------------------------
    identity_ok, identity_feedback = _validate_identity_columns(tree)
    if not identity_ok:
        logger.warning("identity rule rejected generated SQL: %s", identity_feedback)
        return reject(identity_feedback)

    agg_ok, agg_feedback = _validate_aggregation_rules(tree)
    if not agg_ok:
        return reject(agg_feedback)

    store_filter_ok, store_filter_feedback = _validate_store_name_filters(tree)
    if not store_filter_ok:
        return reject(store_filter_feedback)

    category_filter_ok, category_filter_feedback = _validate_category_name_filters(tree)
    if not category_filter_ok:
        return reject(category_filter_feedback)

    # --- table allow-list and shape ---------------------------------------
    referenced = _extract_referenced_tables(tree)

    tables_ok, tables_feedback = _validate_tables(referenced)
    if not tables_ok:
        return reject(tables_feedback)

    alias_ok, alias_feedback = _validate_aliases(tree)
    if not alias_ok:
        return reject(alias_feedback)

    period_ok, period_feedback = _validate_period_filter(tree, referenced)
    if not period_ok:
        return reject(period_feedback)

    columns_ok, columns_feedback = _validate_columns_exist(tree, referenced)
    if not columns_ok:
        return reject(columns_feedback)

    grain_ok, grain_feedback = _validate_customer_grain(tree)
    if not grain_ok:
        logger.warning("customer-grain rule rejected generated SQL: %s", grain_feedback)
        return reject(grain_feedback)

    return SqlValidationResult(is_valid=True, normalized_sql=normalized, feedback="")


def describe_validation_rules() -> str:
    """Rule summary for the planner prompt, so it does not have to guess."""
    return "\n".join(
        [
            "SQL rules (enforced deterministically -- violations are rejected and retried):",
            "- One statement. SELECT or WITH only. No semicolons.",
            "- No SELECT *. Name every column.",
            f"- Query these views UNQUALIFIED: {', '.join(ALLOWED_VIEWS)}.",
            f"- Joinable dimensions: {', '.join(JOINABLE_DIMENSIONS)}.",
            "- Never schema-qualify a table. The database resolves each view to the "
            "caller's permitted variant; qualifying it bypasses that.",
            "- `customer_key` must never reach the OUTERMOST SELECT list or GROUP BY, and "
            "never sit inside an aggregate other than COUNT(DISTINCT). Inside a CTE or "
            "subquery it is free -- group by it to compute a per-customer figure, then "
            "aggregate it away outside.",
            "- Never SUM or AVG `customer_count`: it is distinct customers at one row grain "
            "only. Use COUNT(DISTINCT customer_key) on a customer view for period totals.",
            "- Always filter on the table's period column (calendar_date or month_start_date).",
            "- Never use a reserved word as a table alias. `dim_opco AS do` is a syntax "
            "error; use `o`. Prefer o/s/c/f style aliases.",
            "- Never filter on `store_name` in WHERE. Use resolved `store_id` plus "
            "`opco_code`, or ask a clarification question with branch names if the "
            "branch was not resolved. Do not ask users for IDs.",
            "- Never filter on `category_name` in WHERE either. Use the resolved "
            "`category_key` / `category_lN_key`. If the product or category the user "
            "named was not resolved, do NOT guess a name literal -- a name that "
            "matches no row returns an empty result that reads as a real zero. "
            "Return needs_clarification and ask which category they mean.",
            "- Compute ratios at query time. There are no stored average or percentage columns.",
            "- Category ancestor columns are named differently on either side of a join: "
            "`dim_product_category` has `l1_key..l4_key` (and `l1_name..l4_name`), while "
            "the serving views have `category_l1_key..category_l4_key`. Usually you need "
            "neither -- resolved lookup context already gives you the key to filter on, "
            "so joining dim_product_category just to reach a category is unnecessary.",
        ]
    )
