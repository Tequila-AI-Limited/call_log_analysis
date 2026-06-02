"""Historical report backfill utility.

Parses all existing HTML reports in ``reports/`` and extracts the call
metrics embedded in their narrative text, then upserts those metrics into the
``weekly_stats`` database table.

Use this to seed the database from a set of pre-existing HTML reports when
the DB is empty or was corrupted::

    python backfill_data.py
"""

import glob
import os
import re
from datetime import datetime

from weekly_data_manager import initialize_db, save_week_data

REPORTS_DIR = "reports"


def _get_report_date(fname: str) -> datetime:
    """Parse the report date from a filename of the form ``call_report_DD_MM_YYYY.html``.

    Args:
        fname: Basename or full path of the report HTML file.

    Returns:
        A ``datetime`` parsed from the filename, or ``datetime.min`` if the
        pattern does not match (used as a sort key so bad filenames sort
        first).
    """
    match = re.search(r"call_report_(\d{2}_\d{2}_\d{4})\.html", fname)
    if match:
        try:
            return datetime.strptime(match.group(1), "%d_%m_%Y")
        except ValueError:
            pass
    return datetime.min


def extract_section_metrics(content: str, section_name: str) -> dict | None:
    """Extract call metrics for one weekly section from stripped HTML text.

    Looks for a heading like ``This Week (DD/MM/YYYY to DD/MM/YYYY): Received
    X,XXX calls`` and then scrapes the Retail / Trade / Abandoned breakdown
    from the following ~1 500 characters.

    Args:
        content: HTML-stripped, whitespace-collapsed text of the entire
            report.
        section_name: The section heading to search for, e.g. ``"This Week"``
            or ``"Last Week"``.

    Returns:
        A metrics ``dict`` with keys ``start_date``, ``end_date``, ``total``,
        ``retail``, ``trade``, ``abandoned``, ``abandoned_retail``, and
        ``abandoned_trade``, or ``None`` if the section is not found.
    """
    header_re = re.compile(
        re.escape(section_name)
        + r"\s*\(\s*(\d{2}/\d{2}/\d{4})\s*to\s*(\d{2}/\d{2}/\d{4})\s*\)"
          r"\s*:\s*Received\s*([\d,]+)\s*calls",
        re.IGNORECASE,
    )
    match = header_re.search(content)
    if not match:
        return None

    start_date = match.group(1)
    end_date = match.group(2)
    total_calls = int(match.group(3).replace(",", ""))

    window = content[match.end() : match.end() + 1500]

    def _parse_int(pattern: str) -> int:
        m = re.search(pattern, window)
        return int(m.group(1).replace(",", "")) if m else 0

    retail = _parse_int(r"-\s*Retail:\s*([\d,]+)\s*calls")
    trade = _parse_int(r"-\s*Trade:\s*([\d,]+)\s*calls")
    abandoned = _parse_int(r"-\s*Abandoned:\s*([\d,]+)\s*calls")

    split = re.search(r"\(\s*Retail:\s*([\d,]+),\s*Trade:\s*([\d,]+)\s*\)", window)
    if split:
        abandoned_retail = int(split.group(1).replace(",", ""))
        abandoned_trade = int(split.group(2).replace(",", ""))
    else:
        abandoned_retail = abandoned
        abandoned_trade = 0

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total": total_calls,
        "retail": retail,
        "trade": trade,
        "abandoned": abandoned,
        "abandoned_retail": abandoned_retail,
        "abandoned_trade": abandoned_trade,
    }


def extract_metrics_from_report(report_path: str) -> list[dict]:
    """Parse an HTML report file and return metrics for each weekly section.

    Strips ``<script>`` / ``<style>`` blocks and all HTML tags from the file,
    then delegates to :func:`extract_section_metrics` for each of the two
    weekly sections (``"This Week"`` and ``"Last Week"``).

    Args:
        report_path: Path to an HTML report file (e.g.
            ``reports/call_report_10_05_2026.html``).

    Returns:
        A list containing zero, one, or two metric dicts (one per successfully
        parsed section).
    """
    with open(report_path, encoding="utf-8") as f:
        raw = f.read()

    # Strip scripts/styles then all remaining tags.
    text = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", "", raw, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    results = []
    for section in ("This Week", "Last Week"):
        metrics = extract_section_metrics(text, section)
        if metrics:
            results.append(metrics)

    return results


def main() -> None:
    """Iterate over all HTML reports and upsert their metrics into the DB."""
    print("Starting backfill process...")
    initialize_db()

    report_files = sorted(
        glob.glob(os.path.join(REPORTS_DIR, "call_report_*.html")),
        key=_get_report_date,
    )

    if not report_files:
        print(f"No reports found in '{REPORTS_DIR}'. Nothing to backfill.")
        return

    count = 0
    for path in report_files:
        print(f"Processing {os.path.basename(path)}...")
        for metrics in extract_metrics_from_report(path):
            print(
                f"  {metrics['start_date']} – {metrics['end_date']}: "
                f"total={metrics['total']}, retail={metrics['retail']}, "
                f"trade={metrics['trade']}, abandoned={metrics['abandoned']}"
            )
            save_week_data(metrics)
            count += 1

    print(f"\nBackfill complete. Saved {count} week record(s).")


if __name__ == "__main__":
    main()
