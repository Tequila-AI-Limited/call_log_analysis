"""Report validation module.

Validates a freshly calculated metrics dict against three criteria before the
HTML report is written:

1. **Arithmetic** – Retail + Trade + Abandoned == Total for each week, and
   Week 1 + Week 2 == Grand Total.
2. **Date ranges** – No overlap between This Week and Last Week; each span is
   exactly 7 days.
3. **Historical consistency** – The current "Last Week" figures match what
   was recorded as "This Week" in the previous report (loaded from the JSON
   log).  Mismatches are surfaced as warnings rather than hard errors.

The public entry point is :func:`validate_report`, which chains all three
checks and returns a Markdown summary string plus a boolean pass/fail flag.
"""

import json
import os
from datetime import datetime


# ---------------------------------------------------------------------------
# Check 1: Arithmetic
# ---------------------------------------------------------------------------


def validate_arithmetic(metrics: dict) -> tuple[bool, list[str]]:
    """Verify that all numeric sub-totals add up correctly.

    Checks:
        * ``week1_retail_total + week1_trade_total + week1_abandoned == week1_calls``
        * ``week2_retail_total + week2_trade_total + week2_abandoned == week2_calls``
        * ``week1_calls + week2_calls == total_calls``

    Args:
        metrics: The metrics dict returned by ``analyze_calls`` (and possibly
            modified by ``generate_report``'s historical override).

    Returns:
        A tuple ``(passed, errors)`` where ``passed`` is ``True`` when all
        checks succeed and ``errors`` is a list of human-readable failure
        messages.
    """
    errors: list[str] = []

    expected_total = metrics["week1_calls"] + metrics["week2_calls"]
    if expected_total != metrics["total_calls"]:
        errors.append(
            f"Total mismatch: {metrics['week1_calls']} + {metrics['week2_calls']}"
            f" = {expected_total} != {metrics['total_calls']}"
        )

    for week, label in ((1, "This Week"), (2, "Last Week")):
        retail = metrics[f"week{week}_retail_total"]
        trade = metrics[f"week{week}_trade_total"]
        abd = metrics.get(f"week{week}_retail_abandoned", 0) + metrics.get(
            f"week{week}_trade_abandoned", 0
        )
        calc = retail + trade + abd
        total = metrics[f"week{week}_calls"]
        if calc != total:
            errors.append(
                f"{label} breakdown mismatch: {retail} + {trade} + {abd}"
                f" = {calc} != {total}"
            )

    return (len(errors) == 0, errors)


# ---------------------------------------------------------------------------
# Check 2: Date ranges
# ---------------------------------------------------------------------------


def validate_date_ranges(metrics: dict) -> tuple[bool, list[str]]:
    """Verify that the two week windows do not overlap and are exactly 7 days.

    Args:
        metrics: The metrics dict (requires ``this_week_start``,
            ``this_week_end``, ``last_week_start``, ``last_week_end`` as
            ``YYYY-MM-DD`` strings).

    Returns:
        A tuple ``(passed, errors)`` where ``passed`` is ``True`` when all
        checks succeed.
    """
    errors: list[str] = []

    def _parse(key: str) -> datetime:
        return datetime.strptime(metrics[key], "%Y-%m-%d")

    this_start = _parse("this_week_start")
    this_end = _parse("this_week_end")
    last_start = _parse("last_week_start")
    last_end = _parse("last_week_end")

    if last_end >= this_start:
        errors.append(
            f"Week overlap detected:\n"
            f"  Last Week: {metrics['last_week_start']} to {metrics['last_week_end']}\n"
            f"  This Week: {metrics['this_week_start']} to {metrics['this_week_end']}"
        )

    this_days = (this_end - this_start).days + 1
    last_days = (last_end - last_start).days + 1
    if this_days != 7:
        errors.append(f"This Week spans {this_days} days (expected 7)")
    if last_days != 7:
        errors.append(f"Last Week spans {last_days} days (expected 7)")

    return (len(errors) == 0, errors)


# ---------------------------------------------------------------------------
# Check 3: Historical consistency
# ---------------------------------------------------------------------------


def validate_historical_consistency(metrics: dict) -> tuple[bool, list[str]]:
    """Compare current Last Week figures against the historical JSON log.

    Reads ``reports/historical_weeks.json`` and finds the entry where
    ``this_week.start_date`` matches the current ``last_week_start``.  Any
    numeric differences are returned as *warnings* (non-fatal) so the report
    still generates even if data has been corrected after the fact.

    Args:
        metrics: The metrics dict containing ``last_week_start``,
            ``last_week_end``, and the ``week2_*`` metric fields.

    Returns:
        A tuple ``(True, warnings)`` — always ``True`` because mismatches are
        treated as warnings, not hard errors.  ``warnings`` is an empty list
        when everything matches.
    """
    warnings: list[str] = []

    history_file = os.path.join("reports", "historical_weeks.json")
    if not os.path.exists(history_file):
        return (True, [])

    try:
        with open(history_file, encoding="utf-8") as f:
            history = json.load(f)

        current_last_start = metrics["last_week_start"]
        current_last_end = metrics["last_week_end"]

        # Find the report where this period was recorded as "This Week".
        match = next(
            (
                r["this_week"]
                for r in history.get("reports", [])
                if r.get("this_week", {}).get("start_date") == current_last_start
                and r.get("this_week", {}).get("end_date") == current_last_end
            ),
            None,
        )

        if match:
            checks = [
                ("Total Calls", metrics["week2_calls"], match.get("total", 0)),
                ("Retail Calls", metrics["week2_retail_total"], match.get("retail", 0)),
                ("Trade Calls", metrics["week2_trade_total"], match.get("trade", 0)),
                (
                    "Abandoned Calls",
                    metrics.get("week2_retail_abandoned", 0)
                    + metrics.get("week2_trade_abandoned", 0),
                    match.get("abandoned", 0),
                ),
            ]
            for name, current_val, historic_val in checks:
                if current_val != historic_val:
                    diff = current_val - historic_val
                    warnings.append(
                        f"Historical mismatch — {name}: current={current_val}, "
                        f"historical={historic_val} (diff {diff:+d})"
                    )

    except Exception as exc:
        warnings.append(f"Could not validate history: {exc}")

    return (True, warnings)


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------


def generate_verification_report(metrics: dict) -> tuple[str, bool]:
    """Build a detailed Markdown verification summary for the current report.

    Runs all three validation checks, formats the results into a Markdown
    document, and returns it together with the overall pass/fail flag.

    Args:
        metrics: The fully resolved metrics dict (post-historical-override).

    Returns:
        A tuple ``(markdown_text, passed)`` where ``passed`` is ``True`` only
        when arithmetic and date-range checks both pass (historical warnings
        do not cause a failure).
    """
    lines: list[str] = []
    lines.append("# Report Verification Summary")
    lines.append(f"**Generated:** {datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append("")

    # ---- Check 1: Arithmetic ------------------------------------------------
    arith_ok, arith_errors = validate_arithmetic(metrics)
    lines.append("## 1. Arithmetic Consistency")
    if arith_ok:
        lines.append("- [x] **PASSED** — All sub-components sum correctly to totals.")
    else:
        lines.append("- [ ] **FAILED**")
        for err in arith_errors:
            lines.append(f"  - {err}")
    lines.append("")

    # ---- Check 2: Date ranges -----------------------------------------------
    date_ok, date_errors = validate_date_ranges(metrics)
    lines.append("## 2. Date Ranges")
    if date_ok:
        lines.append(
            f"- [x] **PASSED** — No overlap; each week is exactly 7 days.\n"
            f"  - This Week: {metrics['this_week_start']} to {metrics['this_week_end']}\n"
            f"  - Last Week: {metrics['last_week_start']} to {metrics['last_week_end']}"
        )
    else:
        lines.append("- [ ] **FAILED**")
        for err in date_errors:
            lines.append(f"  - {err}")
    lines.append("")

    # ---- Check 3: Historical consistency ------------------------------------
    _, hist_warnings = validate_historical_consistency(metrics)
    lines.append("## 3. Historical Consistency")
    if hist_warnings:
        lines.append("⚠️ **Warnings** (differences from the previous report)")
        for w in hist_warnings:
            lines.append(f"- {w}")
        lines.append(
            "\n> Differences often reflect data corrections or code updates."
        )
    else:
        lines.append("✅ **Consistent** with historical records.")
    lines.append("")

    # ---- Detailed breakdown -------------------------------------------------
    lines.append("## 4. Metrics Breakdown")
    for week, label, start_key, end_key in (
        (1, "THIS WEEK", "this_week_start", "this_week_end"),
        (2, "LAST WEEK", "last_week_start", "last_week_end"),
    ):
        retail = metrics[f"week{week}_retail_total"]
        trade = metrics[f"week{week}_trade_total"]
        abd = metrics.get(f"week{week}_retail_abandoned", 0) + metrics.get(
            f"week{week}_trade_abandoned", 0
        )
        total = metrics[f"week{week}_calls"]
        check = "✓" if (retail + trade + abd) == total else "❌"
        lines.append(
            f"### {label} ({metrics[start_key]} to {metrics[end_key]})\n"
            f"Retail: {retail:,} | Trade: {trade:,} | Abandoned: {abd:,} | "
            f"**Total: {total:,}** {check}"
        )
        lines.append("")

    overall_total = metrics["week1_calls"] + metrics["week2_calls"]
    lines.append(
        f"### OVERALL TOTAL\n{overall_total:,} calls "
        f"({metrics['week1_calls']:,} + {metrics['week2_calls']:,})"
    )
    lines.append("")

    # ---- Final verdict -------------------------------------------------------
    passed = arith_ok and date_ok
    lines.append("## 5. Final Result")
    if passed:
        lines.append("### ✅ VERIFICATION SUCCESSFUL")
        lines.append("The report is internally consistent and ready for distribution.")
    else:
        lines.append("### ❌ VERIFICATION FAILED")
        lines.append("Discrepancies found above.  Report generation has been aborted.")

    return "\n".join(lines), passed


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_report(results: dict) -> tuple[str, bool]:
    """Run all validation checks on an ``analyze_calls`` result dict.

    This is the only function called by ``generate_report.py``.

    Args:
        results: The dict returned by ``analyze_calls``.  Only
            ``results['metrics']`` is inspected.

    Returns:
        A tuple ``(markdown_text, passed)`` suitable for writing to
        ``reports/report_verification_summary.md`` and deciding whether to
        continue with report generation.
    """
    return generate_verification_report(results["metrics"])
