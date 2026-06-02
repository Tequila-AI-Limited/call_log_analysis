"""Report generation orchestrator.

Ties together every stage of the pipeline to produce the weekly HTML report:

1. **Analyse** — loads and cleans call logs, calculates metrics.
2. **Historical override** — replaces Last Week metrics with verified figures
   from the database when available, ensuring consecutive reports always show
   the same numbers for the same calendar week.
3. **Validate** — checks arithmetic and date-range consistency; aborts if
   anything is wrong.
4. **Persist** — saves This Week and Last Week stats to the DB so future
   reports can reference them.
5. **Render** — fills the Jinja2 template and writes an HTML report to
   ``reports/``.

Entry point (CLI)::

    python generate_report.py [--date YYYY-MM-DD]

The ``--date`` flag is used to back-fill a missing week by capping the data
at a specific Saturday.  Omit it to use the latest date in the data files.
"""

import argparse
import os
from datetime import datetime

from jinja2 import Environment, FileSystemLoader

import weekly_data_manager
from call_log_analyzer import analyze_calls
from store_snapshot import create_snapshot_table, store_snapshot
from validate_historical import validate_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_date(ymd_str: str) -> str:
    """Convert a ``YYYY-MM-DD`` string to ``DD/MM/YYYY`` for display.

    Args:
        ymd_str: Date string in ``YYYY-MM-DD`` format.

    Returns:
        Date string in ``DD/MM/YYYY`` format, or the original string if
        parsing fails.
    """
    if not ymd_str:
        return "N/A"
    try:
        return datetime.strptime(ymd_str, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return ymd_str


def _build_narrative(metrics: dict) -> str:
    """Render the HTML executive-summary narrative from the final metrics dict.

    This function is called *after* any historical override has been applied,
    so the numbers in the narrative are always consistent with the data cards
    and the verification report.

    Args:
        metrics: The fully resolved metrics dict.

    Returns:
        An HTML string suitable for injecting into the Jinja2 template.
    """
    w1_start = _fmt_date(metrics["this_week_start"])
    w1_end = _fmt_date(metrics["this_week_end"])
    w2_start = _fmt_date(metrics["last_week_start"])
    w2_end = _fmt_date(metrics["last_week_end"])

    w1_abd = metrics.get("week1_retail_abandoned", 0) + metrics.get("week1_trade_abandoned", 0)
    w2_abd = metrics.get("week2_retail_abandoned", 0) + metrics.get("week2_trade_abandoned", 0)

    return f"""
    Received a total of <b>{metrics['total_calls']:,}</b> calls across This Week and Last Week.
    <br><br>
    <b>This Week</b> ({w1_start} to {w1_end}): Received {metrics['week1_calls']:,} calls total.
    <br>
    - Retail: {metrics.get('week1_retail_total', 0):,} calls
    <br>
    - Trade: {metrics.get('week1_trade_total', 0):,} calls
    <br>
    - Abandoned: {w1_abd:,} calls
      (Retail: {metrics.get('week1_retail_abandoned', 0):,}, Trade: {metrics.get('week1_trade_abandoned', 0):,})
    <br><br>
    <b>Last Week</b> ({w2_start} to {w2_end}): Received {metrics['week2_calls']:,} calls total.
    <br>
    - Retail: {metrics.get('week2_retail_total', 0):,} calls
    <br>
    - Trade: {metrics.get('week2_trade_total', 0):,} calls
    <br>
    - Abandoned: {w2_abd:,} calls
      (Retail: {metrics.get('week2_retail_abandoned', 0):,}, Trade: {metrics.get('week2_trade_abandoned', 0):,})
    <br><br>
    <b>Out of Hours Analysis:</b>
    <br>
    Operating Hours: Mon-Fri 8am-8pm, Sat 8am-6pm, Sun 10am-4pm
    <br>
    - <b>Total OOH Calls:</b> {metrics.get('ooh_total', 0):,} calls received outside operating hours
    <br>
    - <b>Before Opening:</b> {metrics.get('ooh_before_opening', 0):,} calls
    <br>
    - <b>After Closing:</b> {metrics.get('ooh_after_closing', 0):,} calls
    <br><br>
    <i>Abandoned Call Details (from abandoned logs):</i>
    <br>
    - <b>Agents Logged Out:</b> {metrics.get('abd_agent_logged_out', 0):,} abandoned calls when no agents were logged in
      <br>&nbsp;&nbsp;&nbsp;&nbsp;(Before Opening: {metrics.get('abd_logged_out_before_hours', 0):,},
      During Business Hours: {metrics.get('abd_logged_out_during_hours', 0):,},
      After Closing: {metrics.get('abd_logged_out_after_hours', 0):,})
    <br>
    - <b>Zero Polling:</b> {metrics.get('abd_zero_polling', 0):,} abandoned calls with 0 polling attempts
      (system couldn't reach any agent — typically when all agents are busy or offline).
    """


def _apply_historical_override(results: dict) -> list[str]:
    """Overwrite Last Week metrics with verified figures from the database.

    When the database holds a previously validated row for the Last Week date
    range, those numbers are used instead of the freshly recalculated ones.
    This guarantees that consecutive reports show identical figures for the
    same calendar week regardless of when they are run.

    A hardcoded fallback is applied for the specific week of 2026-04-06 to
    2026-04-12 which was missing from the database at the time of the fix.

    Args:
        results: The dict returned by ``analyze_calls``.  Modified in place.

    Returns:
        A list of human-readable log messages describing what was done.
    """
    log: list[str] = []
    metrics = results["metrics"]
    last_week_start = metrics["last_week_start"]
    last_week_end = metrics["last_week_end"]

    historical_data = weekly_data_manager.load_week_data(last_week_start, last_week_end)

    if historical_data:
        log.append(
            f"[INFO] Historical match found for Last Week ({last_week_start} to "
            f"{last_week_end}). Overwriting with verified DB data."
        )
        log.append(
            f"       Old Total: {metrics['week2_calls']} -> New Total: {int(historical_data['total_calls'])}"
        )

        metrics["week2_calls"] = int(historical_data["total_calls"])
        metrics["week2_retail_total"] = int(historical_data["retail_calls"])
        metrics["week2_retail_calls"] = int(historical_data["retail_calls"])
        metrics["week2_trade_total"] = int(historical_data["trade_calls"])
        metrics["week2_trade_calls"] = int(historical_data["trade_calls"])
        metrics["week2_retail_abandoned"] = int(historical_data["retail_abandoned"])
        metrics["week2_trade_abandoned"] = int(historical_data["trade_abandoned"])

    elif last_week_start == "2026-04-06" and last_week_end == "2026-04-12":
        # One-time fallback for a week whose DB row was missing at the time
        # of the original fix.  Remove once load_week_data returns data for
        # this period reliably.
        log.append(
            f"[INFO] Applying hardcoded fallback for Last Week ({last_week_start} to "
            f"{last_week_end}) — DB row was absent."
        )
        metrics["week2_calls"] = 2343
        metrics["week2_retail_total"] = 1703
        metrics["week2_retail_calls"] = 1703
        metrics["week2_trade_total"] = 366
        metrics["week2_trade_calls"] = 366
        metrics["week2_retail_abandoned"] = 256
        metrics["week2_trade_abandoned"] = 18

    else:
        log.append(
            f"No historical match for Last Week ({last_week_start} to "
            f"{last_week_end}). Using calculated values from raw logs."
        )

    metrics["total_calls"] = metrics["week1_calls"] + metrics["week2_calls"]

    # Recompute week2 abandonment rates from the final (possibly overridden) counts.
    w2_retail = metrics.get("week2_retail_calls", 0)
    w2_trade = metrics.get("week2_trade_calls", 0)
    w2_retail_abd = metrics.get("week2_retail_abandoned", 0)
    w2_trade_abd = metrics.get("week2_trade_abandoned", 0)
    metrics["week2_retail_abandonment_rate"] = (
        round(w2_retail_abd / w2_retail * 100, 1) if w2_retail > 0 else 0.0
    )
    metrics["week2_trade_abandonment_rate"] = (
        round(w2_trade_abd / w2_trade * 100, 1) if w2_trade > 0 else 0.0
    )

    return log


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate_report(target_date: str | None = None) -> None:
    """Run the full report generation pipeline.

    Args:
        target_date: Optional ceiling date in ``YYYY-MM-DD`` format.  When
            supplied, only call records on or before this date are included.
            Use this to back-fill a missing historical week by passing the
            Saturday that ended that week.  Defaults to the latest date
            present in the data files.
    """
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    print(f"Running analysis (target date: {target_date or 'latest'})...")
    results = analyze_calls(data_dir, target_max_date=target_date)

    if not results:
        print("Analysis failed or returned no results.")
        return

    # ---- Stage 1: Apply historical override ---------------------------------
    print("Checking for historical data in DB...")
    for msg in _apply_historical_override(results):
        print(msg)

    # ---- Stage 2: Validate --------------------------------------------------
    print("Validating metrics...")
    os.makedirs("reports", exist_ok=True)
    validation_message, validation_passed = validate_report(results)

    with open("reports/report_verification_summary.md", "w", encoding="utf-8") as f:
        f.write(validation_message)

    if not validation_passed:
        print("\n" + "=" * 60)
        print("ERROR: VALIDATION FAILED — report generation aborted.")
        print("=" * 60)
        print(validation_message)
        print("Validation summary: reports/report_verification_summary.md")
        return

    warnings: list[str] = []

    # ---- Stage 3: Persist This Week and Last Week to DB ---------------------
    metrics = results["metrics"]
    print("\nSaving weekly stats to DB...")

    this_week_row = {
        "start_date": metrics["this_week_start"],
        "end_date": metrics["this_week_end"],
        "total": metrics["week1_calls"],
        "retail": metrics["week1_retail_total"],
        "trade": metrics["week1_trade_total"],
        "abandoned": metrics.get("week1_retail_abandoned", 0) + metrics.get("week1_trade_abandoned", 0),
        "abandoned_retail": metrics.get("week1_retail_abandoned", 0),
        "abandoned_trade": metrics.get("week1_trade_abandoned", 0),
    }
    try:
        weekly_data_manager.save_week_data(this_week_row)
    except Exception as exc:
        warnings.append(f"This Week DB stats NOT saved: {exc}")

    last_week_row = {
        "start_date": metrics["last_week_start"],
        "end_date": metrics["last_week_end"],
        "total": metrics["week2_calls"],
        "retail": metrics["week2_retail_total"],
        "trade": metrics["week2_trade_total"],
        "abandoned": metrics.get("week2_retail_abandoned", 0) + metrics.get("week2_trade_abandoned", 0),
        "abandoned_retail": metrics.get("week2_retail_abandoned", 0),
        "abandoned_trade": metrics.get("week2_trade_abandoned", 0),
    }
    try:
        weekly_data_manager.save_week_data(last_week_row)
    except Exception as exc:
        warnings.append(f"Last Week DB stats NOT saved: {exc}")

    print("Saving report snapshot to DB...")
    try:
        report_date_obj = None
        if target_date:
            report_date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()
        create_snapshot_table()
        store_snapshot(metrics, report_date=report_date_obj)
    except Exception as exc:
        warnings.append(f"DB snapshot NOT saved: {exc}")

    # ---- Stage 4: Render HTML -----------------------------------------------
    results["narrative"] = _build_narrative(metrics)

    print("Rendering HTML report...")
    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("call_report.html.j2")
    html_output = template.render(
        metrics=metrics,
        plots=results["plots"],
        narrative=results["narrative"],
        raw_data=results["raw_data"],
        abandoned_logs=results["abandoned_logs"],
        max_date=results.get("max_date", "N/A"),
        abandoned_trade_customers=results.get(
            "abandoned_trade_customers", {"week1": [], "week2": []}
        ),
    )

    max_date_obj = results.get("max_date_obj")
    filename = (
        f"call_report_{max_date_obj.strftime('%d_%m_%Y')}.html"
        if max_date_obj
        else "call_report.html"
    )
    output_path = os.path.join("reports", filename)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_output)

    # ---- Summary ------------------------------------------------------------
    print(f"\nReport saved: {output_path}")
    print("=" * 60)
    print("REPORT GENERATED — WITH WARNINGS" if warnings else "SUCCESS")
    print("=" * 60)
    print(
        f"This Week: {this_week_row['start_date']} to {this_week_row['end_date']}"
        f" ({this_week_row['total']} calls)"
    )
    print(
        f"Last Week: {metrics['last_week_start']} to {metrics['last_week_end']}"
        f" ({metrics['week2_calls']} calls)"
    )
    print(f"Report:    {output_path}")
    print(f"Verify:    reports/report_verification_summary.md")
    if warnings:
        print("\n" + "!" * 60)
        print(f"  {len(warnings)} NON-FATAL WARNING(S):")
        for w in warnings:
            print(f"  - {w}")
        print("!" * 60)
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate the weekly call log HTML report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Back-filling a missing week:\n"
            "  Pass the Saturday that ended the target week as --date.\n"
            "  Example: python generate_report.py --date 2026-05-10"
        ),
    )
    parser.add_argument(
        "--date",
        "-d",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Ceiling date for data inclusion.  Use the Saturday that ended "
            "the target week.  Defaults to the latest date in the data files."
        ),
    )
    args = parser.parse_args()
    generate_report(target_date=args.date)
