"""Relative time-period context for the planner.

The planner has no notion of "today", so phrases like "last 3 months", "this
year", or "now" would otherwise become a needless clarification question -- "show
me high-value customers who are NOW inactive" came back asking which month was
meant. This resolves the current business date into concrete date ranges the
planner can drop straight into a predicate.

Two conventions, both chosen deliberately: "now" is the present (today, or the
current month on a monthly table), and "last N months" is a rolling window ending
today rather than the last N COMPLETE months.

OPEN ASSUMPTION -- "now" on a monthly table resolves to the CURRENT month, which
assumes fact_customer_opco_monthly carries a row for the month in progress. If the
monthly load instead runs after month close, that row does not exist until the
following month and every "now" question answers 0 on every day of the month. The
symptom is a confident zero rather than an error, so it is worth naming: if that
turns out to be the load pattern, "now" must mean the latest month that HAS data,
which needs the data horizon read when this block is built rather than derived
from the calendar.

v3 changed the shape of the output. v2 emitted event_year / event_month anchors
and YYYYMM integer ranges, because every fact was monthly. The serving tables are
now daily (calendar_date) or monthly (month_start_date), both real dates, so this
emits real date literals. Month arithmetic still happens here rather than in the
model, which is far more reliable across year boundaries.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core import settings


def _shift_month(value: date, delta: int) -> date:
    index = value.year * 12 + (value.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _month_end(value: date) -> date:
    return _shift_month(value, 1) - timedelta(days=1)


def _current_business_date() -> date:
    tz_name = getattr(settings, "copilot_timezone", "Asia/Kuala_Lumpur")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


def build_relative_time_context(today: date | None = None) -> str:
    """The current-date block injected into the planner prompt."""
    today = today or _current_business_date()

    this_month = _month_start(today)
    last_month = _shift_month(this_month, -1)

    # "Last N months" is a ROLLING window ending TODAY, not the last N complete
    # months. These used to end at the last complete month, on the reasoning that a
    # partial current month understates a total -- true, but it is not what the
    # phrase means, and a user asking for the last 3 months on the 30th expects the
    # 30th to be in it. The partial-month caveat is stated in the block instead, so
    # the planner can mention it rather than silently move the boundary.
    # "Last month" is the previous CALENDAR month, not a rolling 30 days -- the
    # singular names a month, the plural names a span. Only the plural rolls.
    def daily(months: int) -> str:
        start = _shift_month(this_month, -months) + timedelta(days=today.day - 1)
        return f"BETWEEN DATE '{start.isoformat()}' AND DATE '{today.isoformat()}'"

    def monthly(months: int) -> str:
        # Monthly rows are all-or-nothing, so a rolling window becomes the N months
        # up to and including the current one.
        start = _shift_month(this_month, -(months - 1))
        return f"BETWEEN DATE '{start.isoformat()}' AND DATE '{this_month.isoformat()}'"

    # Previous full calendar quarter.
    q_start_month = ((today.month - 1) // 3) * 3 + 1
    this_q = date(today.year, q_start_month, 1)
    lq_start = _shift_month(this_q, -3)
    lq_end_month = _shift_month(this_q, -1)

    ytd_start = date(today.year, 1, 1)
    ly_start = date(today.year - 1, 1, 1)
    # Same period last year, calendar-aligned. Used for year-over-year questions.
    ly_same_day = date(today.year - 1, today.month, today.day) if not (
        today.month == 2 and today.day == 29
    ) else date(today.year - 1, 2, 28)

    return "\n".join(
        [
            f"Today's date: {today.isoformat()}",
            f"Current month (partial -- today is day {today.day}): {this_month.isoformat()}",
            f"Last complete month: {last_month.isoformat()} .. {_month_end(last_month).isoformat()}",
            "",
            "NOW / CURRENTLY / RIGHT NOW / AS OF TODAY / AT THE MOMENT mean the present,",
            "never the last complete month. Never ask which period the user meant when the",
            "question says any of those -- it has already told you.",
            f"- daily tables:   calendar_date = DATE '{today.isoformat()}'",
            f"- monthly tables: month_start_date = DATE '{this_month.isoformat()}'",
            "A present-tense question with no period at all -- \"who is inactive\", \"how many",
            "are Elite\" -- means the same thing: the current month.",
            "",
            "The current month is PARTIAL. When a range includes it, say so in `reason` if",
            "the figure is a total that will keep rising; do not silently shorten the range",
            "to avoid it.",
            "",
            "Daily tables filter on calendar_date. Ready-to-use ranges:",
            f"- last month:      calendar_date BETWEEN DATE '{last_month.isoformat()}' "
            f"AND DATE '{_month_end(last_month).isoformat()}'",
            f"- last 3 months:   calendar_date {daily(3)}",
            f"- last 6 months:   calendar_date {daily(6)}",
            f"- last 12 months:  calendar_date {daily(12)}",
            f"- so far this month: calendar_date BETWEEN DATE '{this_month.isoformat()}' "
            f"AND DATE '{today.isoformat()}'",
            f"- last quarter:    calendar_date BETWEEN DATE '{lq_start.isoformat()}' "
            f"AND DATE '{_month_end(lq_end_month).isoformat()}'",
            f"- this year to date: calendar_date BETWEEN DATE '{ytd_start.isoformat()}' "
            f"AND DATE '{today.isoformat()}'",
            "",
            "Monthly tables filter on month_start_date, which is always the 1st. Ranges:",
            f"- this month / now: month_start_date = DATE '{this_month.isoformat()}'",
            f"- last month:      month_start_date = DATE '{last_month.isoformat()}'",
            f"- last 3 months:   month_start_date {monthly(3)}",
            f"- last 12 months:  month_start_date {monthly(12)}",
            "",
            "Year-over-year: pass BOTH ranges explicitly, never derive the prior year with",
            "date arithmetic and never filter on day-of-year (it drifts across a leap year).",
            f"- this year to date: DATE '{ytd_start.isoformat()}' .. DATE '{today.isoformat()}'",
            f"- same period last year: DATE '{ly_start.isoformat()}' .. DATE '{ly_same_day.isoformat()}'",
        ]
    )
