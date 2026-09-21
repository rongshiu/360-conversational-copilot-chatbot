# app/service/metric_service.py
"""Metric registry: one definition per metric, filtered by role.

Solves a specific failure mode. "Membership penetration" can legitimately mean
share of transactions, share of sales value, or share of distinct customers.
Without a registry the planner picks one per turn based on whatever columns the
schema context happened to surface, so the same question asked twice can return
different numbers with nothing in either answer saying which definition was used.

Pinning it also fixes the reverse problem: v2 stored `avg_basket_value` as a
pre-divided column, so SUM() and AVG() over it were both wrong. Every ratio here
is a numerator and a denominator, divided at query time.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

from app.core import settings
from app.core.logging import Logger
from app.utils.text_utils import normalize_lookup_text, safe_schema_name

logger = Logger.get_logger(__name__)


@dataclass(frozen=True)
class MetricDefinition:
    metric_key: str
    metric_name: str
    metric_class: str
    min_role_level: str
    base_table: str
    numerator_sql: str
    denominator_sql: Optional[str]
    is_additive: bool
    grain_note: Optional[str]
    synonyms: tuple[str, ...]
    description: Optional[str]

    @property
    def is_ratio(self) -> bool:
        return self.denominator_sql is not None

    @property
    def needs_money(self) -> bool:
        return self.metric_class in {"money", "ratio_money"}

    def as_sql_expression(self) -> str:
        """The metric as a single SQL expression."""
        if not self.is_ratio:
            return self.numerator_sql
        return f"({self.numerator_sql}) / NULLIF({self.denominator_sql}, 0)"

    def describe(self) -> str:
        lines = [f"{self.metric_key} -- {self.metric_name}"]
        if self.is_ratio:
            lines.append(f"    = ({self.numerator_sql}) / NULLIF({self.denominator_sql}, 0)")
        else:
            lines.append(f"    = {self.numerator_sql}")
        lines.append(f"    from: {self.base_table}")
        if self.grain_note:
            lines.append(f"    note: {self.grain_note}")
        return "\n".join(lines)


@dataclass(frozen=True)
class WithheldMetric:
    """A money metric a caller asked for but may not use, and its stand-in.

    `substitute` is None when nothing volume-based answers the same question --
    average basket VALUE has no honest equivalent, because units per basket is a
    different quantity rather than the same one in other clothes. That distinction
    is what separates a reportable substitution from a refusal.
    """

    requested: "MetricDefinition"
    substitute: Optional["MetricDefinition"]

    @property
    def has_substitute(self) -> bool:
        return self.substitute is not None and not self.substitute.needs_money


class MetricService:
    def __init__(self) -> None:
        self.metrics: dict[str, MetricDefinition] = {}
        # synonym -> ALL metrics claiming it, in registry order. Several metrics
        # deliberately share a synonym so a role can fall back to a volume
        # variant; a one-to-one map would make the fallback unreachable.
        self._by_synonym: dict[str, list[str]] = {}
        self.source = "unknown"
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            rows = self._load_from_db()
        except Exception as exc:  # noqa: BLE001 - degrade rather than fail startup
            logger.warning(
                "metric_definition read failed (%s); falling back to the seed CSV", exc
            )
            rows = self._load_from_csv()
            self.source = "csv-fallback"
        else:
            if rows:
                self.source = "postgres"
            else:
                logger.warning(
                    "metric_definition is empty; falling back to the seed CSV. Run "
                    "`python -m app.scripts.load_metric_definitions` to populate it."
                )
                rows = self._load_from_csv()
                self.source = "csv-fallback"

        for row in rows:
            metric = MetricDefinition(
                metric_key=str(row["metric_key"]),
                metric_name=str(row.get("metric_name") or row["metric_key"]),
                metric_class=str(row.get("metric_class") or "volume"),
                min_role_level=str(row.get("min_role_level") or "EXEC").upper(),
                base_table=str(row.get("base_table") or ""),
                numerator_sql=str(row.get("numerator_sql") or ""),
                denominator_sql=(str(row["denominator_sql"]) if row.get("denominator_sql") else None),
                is_additive=bool(row.get("is_additive")),
                grain_note=(str(row["grain_note"]) if row.get("grain_note") else None),
                synonyms=tuple(_coerce_synonyms(row.get("synonyms"))),
                description=(str(row["description"]) if row.get("description") else None),
            )
            self.metrics[metric.metric_key] = metric
            for synonym in (metric.metric_name, *metric.synonyms):
                norm = normalize_lookup_text(synonym)
                if norm:
                    self._by_synonym.setdefault(norm, []).append(metric.metric_key)

        logger.info("metric registry loaded from %s: %d metrics", self.source, len(self.metrics))

    def _load_from_db(self) -> list[dict[str, Any]]:
        from app.db.postgres import get_pg_conn

        schema = safe_schema_name(settings.ci_meta_schema)
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT metric_key, metric_name, metric_class, min_role_level,
                           base_table, numerator_sql, denominator_sql, is_additive,
                           grain_note, synonyms, description
                    FROM {schema}.metric_definition
                    WHERE is_active = true
                    ORDER BY metric_key
                    """
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, rec)) for rec in cur.fetchall()]

    def _load_from_csv(self) -> list[dict[str, Any]]:
        import pandas as pd

        path = getattr(settings, "metric_csv_path", "/app/data/v3_metric_definition_seed.csv")
        try:
            df = pd.read_csv(path).rename(columns=lambda c: str(c).strip())
        except Exception as exc:  # noqa: BLE001
            logger.warning("metric seed CSV unreadable at %s (%s); registry is empty", path, exc)
            return []
        records = df.where(df.notna(), None).to_dict("records")
        for r in records:
            r["is_additive"] = str(r.get("is_additive")).strip().upper() in {"TRUE", "1", "YES"}
        return records

    # ------------------------------------------------------------------
    def available(self, principal=None) -> list[MetricDefinition]:
        """Metrics this caller may use.

        A money metric is withheld from a non-HOD caller entirely rather than
        offered and then failing: the executive views do not have the column, so
        the SQL would be a hard error.
        """
        can_see_money = bool(getattr(principal, "can_see_money", True)) if principal else True
        return [
            m
            for m in self.metrics.values()
            if can_see_money or m.min_role_level != "HOD"
        ]

    def resolve(self, phrase: str, principal=None) -> Optional[MetricDefinition]:
        """Map a user phrase to a metric the caller may actually use.

        Matching is restricted to the caller's available metrics rather than
        matching first and rejecting afterwards. Several metrics deliberately share
        a synonym so a role can fall back: "best daypart" matches both
        best_daypart_by_sales (money, HOD) and best_daypart_by_txn (volume). Match-
        then-reject handed an executive None; searching within their own set hands
        them the volume variant, which answers the question perfectly well.
        """
        norm = normalize_lookup_text(phrase)
        if not norm:
            return None

        allowed = {m.metric_key for m in self.available(principal)}
        if not allowed:
            return None

        can_see_money = bool(getattr(principal, "can_see_money", True)) if principal else True
        key = self._match(norm, allowed, prefer_money=can_see_money)
        return self.metrics.get(key) if key else None

    def withheld_for(self, phrase: str, principal=None) -> Optional["WithheldMetric"]:
        """The money metric this question asked for, when the caller may not see it.

        `resolve` deliberately searches only within the caller's own set, so an
        executive asking about sales gets the volume twin and never learns that a
        substitution happened. That is the right answer and the wrong silence: a
        transactions figure narrated with no comment reads exactly like the revenue
        figure that was asked for.

        This re-runs the same match against the FULL registry to recover what a HOD
        would have been given, so the substitution can be reported. It reads
        nothing the caller may not see -- a metric definition is not data -- and it
        never reaches a fact table.

        Returns None when the caller can see money, when nothing matched, or when
        what matched was a volume metric anyway.
        """
        can_see_money = bool(getattr(principal, "can_see_money", True)) if principal else True
        if can_see_money:
            return None

        norm = normalize_lookup_text(phrase)
        if not norm:
            return None

        key = self._match(norm, set(self.metrics), prefer_money=True)
        requested = self.metrics.get(key) if key else None
        if requested is None or not requested.needs_money:
            return None

        return WithheldMetric(
            requested=requested,
            substitute=self._volume_twin(requested, principal),
        )

    def _volume_twin(self, metric: MetricDefinition, principal=None) -> Optional[MetricDefinition]:
        """A volume metric this caller may use that answers the same question.

        Found by shared synonym rather than by re-running the phrase match, and the
        difference matters: whether a twin exists decides substitute-vs-refuse, so
        deriving it from the fuzzy matcher would let a MISS become a refusal of a
        perfectly legitimate question. "best performing daypart by sales" matches no
        synonym of best_daypart_by_txn as a substring, yet the two are twins.

        Shared synonyms are the registry's own declared fallback mechanism -- see
        the _by_synonym comment: several metrics deliberately claim the same phrase
        so that a role can fall back to a volume variant.

        None means no volume metric answers the same question, which is a genuine
        refusal rather than a matcher failure: average basket VALUE has no volume
        equivalent, because units per basket is a different quantity rather than
        the same one in other clothes.
        """
        allowed = {m.metric_key for m in self.available(principal)}
        if not allowed:
            return None
        for synonym in (metric.metric_name, *metric.synonyms):
            norm = normalize_lookup_text(synonym)
            if not norm:
                continue
            for key in self._by_synonym.get(norm, ()):
                candidate = self.metrics.get(key) if key in allowed else None
                if candidate is not None and not candidate.needs_money:
                    return candidate
        return None

    def _match(self, norm: str, allowed: set[str], *, prefer_money: bool) -> Optional[str]:
        """Longest-synonym match restricted to `allowed`.

        Every tie is resolved by _prefer over the WHOLE tied set, never by
        whichever synonym happened to be indexed first. That distinction is not
        cosmetic: registry order differs by load path -- Postgres reads
        `ORDER BY metric_key`, the CSV fallback reads file order -- so an
        order-dependent tie-break makes the same question resolve to a different
        metric depending on where the registry came from.

        It bit exactly that way. "…combined revenue growth and active member
        overlap…" contains two 7-character synonyms, `revenue` (total_sales, money)
        and `overlap` (cross_opco_overlap, volume). Keeping the first-seen winner
        picked total_sales from the CSV and cross_opco_overlap from Postgres, so the
        deployed service could not see that a money metric had been asked for and
        withheld the substitution notice that should have gone with the answer.
        """
        # Exact synonym hit. When several metrics share the synonym, prefer the
        # money variant for a caller who can see money -- "sale performance" should
        # mean sales for a HOD and transactions for everyone else.
        exact = [k for k in self._by_synonym.get(norm, ()) if k in allowed]
        if exact:
            return self._prefer(exact, prefer_money=prefer_money)

        # Otherwise the longest containing synonym wins, so "membership penetration
        # by sales" beats "penetration". Collect every candidate at the winning
        # length rather than committing to the first synonym that reached it.
        best_len = 0
        tied: list[str] = []
        for synonym, metric_keys in self._by_synonym.items():
            if not synonym or synonym not in norm:
                continue
            candidates = [k for k in metric_keys if k in allowed]
            if not candidates:
                continue
            if len(synonym) > best_len:
                best_len, tied = len(synonym), list(candidates)
            elif len(synonym) == best_len:
                tied.extend(candidates)

        return self._prefer(tied, prefer_money=prefer_money) if tied else None

    def _prefer(self, metric_keys: list[str], *, prefer_money: bool) -> str:
        """Pick the most informative metric from equal matches.

        Sorted first, so the answer does not depend on registry insertion order --
        see _match. Sorting is what makes the Postgres and CSV load paths agree.
        """
        ordered = sorted(set(metric_keys))
        if prefer_money:
            for key in ordered:
                metric = self.metrics.get(key)
                if metric is not None and metric.needs_money:
                    return key
        return ordered[0]

    def format_context(self, principal=None, limit: int = 14) -> str:
        """The metric block for the planner prompt."""
        metrics = self.available(principal)
        if not metrics:
            return ""

        lines = [
            "Metric definitions (use these exact numerators and denominators -- do not "
            "invent your own):",
        ]
        for metric in metrics[:limit]:
            lines.append(metric.describe())

        withheld = [m for m in self.metrics.values() if m not in metrics]
        if withheld:
            lines.append(
                "\nNot available to you (revenue-bearing): "
                + ", ".join(sorted(m.metric_key for m in withheld))
                + ". Answer with a volume equivalent and say the figure is volume-based."
            )
        return "\n".join(lines)


def _coerce_synonyms(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(str(value))
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    except Exception:
        return [p.strip() for p in str(value).split(",") if p.strip()]


@lru_cache(maxsize=1)
def get_metric_service() -> MetricService:
    return MetricService()
