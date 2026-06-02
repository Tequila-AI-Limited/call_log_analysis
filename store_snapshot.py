"""Historical report snapshot storage.

Writes a row to the ``report_snapshots`` PostgreSQL table for every report
run so that week-over-week trends can be queried directly from the database.

Each run stores **two rows** — one for Week 1 (This Week) and one for Week 2
(Last Week) — keyed on ``(report_date, week_number)`` with an UPSERT so
re-running the same report just refreshes the existing rows.

Typical usage (called from ``generate_report.py``)::

    from store_snapshot import create_snapshot_table, store_snapshot
    create_snapshot_table()
    store_snapshot(metrics, report_date=date(2026, 5, 10))
"""

from datetime import date, datetime

import pandas as pd

from db import get_connection


# ---------------------------------------------------------------------------
# Table management
# ---------------------------------------------------------------------------


def create_snapshot_table() -> bool:
    """Create the ``report_snapshots`` table if it does not already exist.

    The table uses ``(report_date, week_number)`` as a unique key so that
    UPSERTs in :func:`store_snapshot` are idempotent.

    Returns:
        ``True`` on success, ``False`` if a database error occurred.
    """
    ddl = """
        CREATE TABLE IF NOT EXISTS report_snapshots (
            id                      SERIAL PRIMARY KEY,
            report_date             DATE    NOT NULL,
            week_number             INTEGER NOT NULL,
            week_label              TEXT,
            week_start_date         DATE,
            week_end_date           DATE,
            total_calls             INTEGER,
            retail_calls            INTEGER,
            trade_calls             INTEGER,
            abandoned_calls         INTEGER,
            answered_calls          INTEGER,
            abandonment_rate        DECIMAL(5,2),
            retail_abandonment_rate DECIMAL(5,2),
            trade_abandonment_rate  DECIMAL(5,2),
            created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (report_date, week_number)
        );
    """
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(ddl)
            print("report_snapshots table ready.")
            return True
        finally:
            conn.close()
    except Exception as exc:
        print(f"Error creating report_snapshots table: {exc}")
        return False


# ---------------------------------------------------------------------------
# Snapshot writing
# ---------------------------------------------------------------------------


def _upsert_week(cursor, report_date: date, week_number: int, label: str, metrics: dict) -> None:
    """Insert or update a single weekly snapshot row.

    Args:
        cursor: An open psycopg2 cursor (within an active transaction).
        report_date: The date the report was generated.
        week_number: 1 for This Week, 2 for Last Week.
        label: Human-readable label (``"This Week"`` / ``"Last Week"``).
        metrics: The full metrics dict from ``analyze_calls``.
    """
    prefix = f"week{week_number}"
    start_key = "this_week_start" if week_number == 1 else "last_week_start"
    end_key = "this_week_end" if week_number == 1 else "last_week_end"
    abd_rate_key = f"week{week_number}_abandonment_rate"

    cursor.execute(
        """
        INSERT INTO report_snapshots (
            report_date, week_number, week_label,
            week_start_date, week_end_date,
            total_calls, retail_calls, trade_calls,
            abandoned_calls, abandonment_rate,
            retail_abandonment_rate, trade_abandonment_rate
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (report_date, week_number) DO UPDATE SET
            week_start_date         = EXCLUDED.week_start_date,
            week_end_date           = EXCLUDED.week_end_date,
            total_calls             = EXCLUDED.total_calls,
            retail_calls            = EXCLUDED.retail_calls,
            trade_calls             = EXCLUDED.trade_calls,
            abandoned_calls         = EXCLUDED.abandoned_calls,
            abandonment_rate        = EXCLUDED.abandonment_rate,
            retail_abandonment_rate = EXCLUDED.retail_abandonment_rate,
            trade_abandonment_rate  = EXCLUDED.trade_abandonment_rate
        """,
        (
            report_date,
            week_number,
            label,
            metrics.get(start_key),
            metrics.get(end_key),
            metrics[f"{prefix}_calls"],
            metrics[f"{prefix}_retail_total"],
            metrics[f"{prefix}_trade_total"],
            metrics.get(f"{prefix}_retail_abandoned", 0)
            + metrics.get(f"{prefix}_trade_abandoned", 0),
            metrics.get(abd_rate_key, metrics.get("abandonment_rate", 0)),
            metrics[f"{prefix}_retail_abandonment_rate"],
            metrics[f"{prefix}_trade_abandonment_rate"],
        ),
    )


def store_snapshot(metrics: dict, report_date: date | None = None) -> bool:
    """Persist metric snapshots for both weeks of a report run.

    Writes two rows to ``report_snapshots`` (Week 1 and Week 2) using an
    UPSERT, so re-running the same report date simply refreshes the values
    rather than creating duplicates.

    Args:
        metrics: The fully resolved metrics dict from ``analyze_calls`` (after
            any historical overrides applied by ``generate_report.py``).
        report_date: The logical report date.  Defaults to today if omitted.

    Returns:
        ``True`` on success, ``False`` if a database error occurred.
    """
    if report_date is None:
        report_date = date.today()

    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    _upsert_week(cur, report_date, 1, "This Week", metrics)
                    _upsert_week(cur, report_date, 2, "Last Week", metrics)
            print(f"Stored snapshots for report date {report_date}.")
            return True
        finally:
            conn.close()
    except Exception as exc:
        print(f"Error storing snapshot: {exc}")
        return False


# ---------------------------------------------------------------------------
# Querying (not used by the main pipeline but useful for ad-hoc analysis)
# ---------------------------------------------------------------------------


def get_previous_report_comparison() -> pd.DataFrame | None:
    """Fetch the previous report's This Week row for trend comparison.

    Returns:
        A single-row DataFrame with columns ``report_date``, ``total_calls``,
        ``retail_calls``, ``trade_calls``, ``abandoned_calls``, and
        ``abandonment_rate``, or ``None`` if no previous report exists.
    """
    query = """
        SELECT report_date, total_calls, retail_calls, trade_calls,
               abandoned_calls, abandonment_rate
        FROM   report_snapshots
        WHERE  week_label = 'This Week'
        ORDER  BY report_date DESC
        LIMIT  1 OFFSET 1
    """
    try:
        conn = get_connection()
        try:
            df = pd.read_sql(query, conn)
            return df if not df.empty else None
        finally:
            conn.close()
    except Exception as exc:
        print(f"Error fetching previous report: {exc}")
        return None


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    create_snapshot_table()
    print("Snapshot table verified successfully.")
