"""Weekly call statistics persistence layer.

Manages the ``weekly_stats`` PostgreSQL table, which stores one aggregated
row per calendar week.  The table is keyed on ``(week_start, week_end)`` so
UPSERT operations are idempotent — re-running a report for the same week
just refreshes the numbers.

This module is used in two ways:

* **Generate report** (``generate_report.py``) — loads last week's verified
  numbers and saves this week's freshly calculated numbers.
* **Backfill** (``backfill_data.py``) — seeds the table from existing HTML
  reports when the DB is empty or was reset.

Typical usage::

    import weekly_data_manager as wdm
    wdm.initialize_db()
    last_week = wdm.load_week_data("2026-04-27", "2026-05-03")
    wdm.save_week_data({
        "start_date": "2026-05-04",
        "end_date":   "2026-05-10",
        "total":      2500,
        "retail":     1800,
        "trade":      400,
        "abandoned":  300,
        "abandoned_retail": 270,
        "abandoned_trade":  30,
    })
"""

from datetime import datetime

import pandas as pd

from db import get_connection

TABLE_NAME = "weekly_stats"


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------


def initialize_db() -> None:
    """Create the ``weekly_stats`` table if it does not already exist.

    Safe to call on every pipeline run — it is a no-op when the table is
    already present.

    Raises:
        Exception: Re-raises any database error so the caller can decide
            whether to abort or warn and continue.
    """
    ddl = f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            id                   SERIAL PRIMARY KEY,
            week_start           VARCHAR(20) NOT NULL,
            week_end             VARCHAR(20) NOT NULL,
            total_calls          INTEGER,
            retail_calls         INTEGER,
            trade_calls          INTEGER,
            abandoned_total      INTEGER,
            retail_abandoned     INTEGER,
            trade_abandoned      INTEGER,
            report_generated_date TIMESTAMP,
            UNIQUE (week_start, week_end)
        );
    """
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _normalise_date(value: str | datetime) -> str:
    """Coerce a date to an ISO ``YYYY-MM-DD`` string for DB comparison.

    Args:
        value: A ``datetime`` object or a string in ``YYYY-MM-DD`` or
            ``DD/MM/YYYY`` format.

    Returns:
        Date string in ``YYYY-MM-DD`` format.
    """
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str) and "/" in value:
        try:
            return datetime.strptime(value, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return value


def load_week_data(start_date: str | datetime, end_date: str | datetime) -> dict | None:
    """Retrieve the stored metrics for a specific week from the database.

    Args:
        start_date: Start of the week (inclusive) as ``YYYY-MM-DD`` string or
            ``datetime``.
        end_date: End of the week (inclusive) as ``YYYY-MM-DD`` string or
            ``datetime``.

    Returns:
        A dict of column values if a matching row is found, otherwise
        ``None``.
    """
    from psycopg2.extras import RealDictCursor

    start_date = _normalise_date(start_date)
    end_date = _normalise_date(end_date)

    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {TABLE_NAME} WHERE week_start = %s AND week_end = %s",
                (start_date, end_date),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as exc:
        print(f"Error loading week data from DB: {exc}")
        return None
    finally:
        if conn:
            conn.close()


def get_all_weeks() -> list[dict]:
    """Return all stored weekly records, sorted by ``week_start`` ascending.

    Returns:
        A list of dicts, one per row in ``weekly_stats``, or an empty list
        if the query fails.
    """
    try:
        conn = get_connection()
        try:
            df = pd.read_sql(
                f"SELECT * FROM {TABLE_NAME} ORDER BY week_start", conn
            )
            return df.to_dict("records")
        finally:
            conn.close()
    except Exception as exc:
        print(f"Error retrieving all weeks: {exc}")
        return []


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def save_week_data(metrics: dict) -> None:
    """Upsert weekly metrics into the ``weekly_stats`` table.

    If a row for ``(week_start, week_end)`` already exists it is updated in
    place; otherwise a new row is inserted.  This makes re-running a report
    safe — it will not create duplicate entries.

    Args:
        metrics: A dict containing at minimum:

            * ``start_date`` (``str``) – ``YYYY-MM-DD`` week start.
            * ``end_date`` (``str``) – ``YYYY-MM-DD`` week end.
            * ``total`` (``int``) – Total calls for the week.
            * ``retail`` (``int``) – Retail calls.
            * ``trade`` (``int``) – Trade calls.
            * ``abandoned`` (``int``) – Total abandoned calls.
            * ``abandoned_retail`` (``int``) – Retail abandoned split.
            * ``abandoned_trade`` (``int``) – Trade abandoned split.

    Raises:
        ConnectionError: If the database cannot be reached.
        Exception: Re-raises unexpected database errors after rolling back.
    """
    initialize_db()

    upsert_sql = f"""
        INSERT INTO {TABLE_NAME}
            (week_start, week_end, total_calls, retail_calls, trade_calls,
             abandoned_total, retail_abandoned, trade_abandoned, report_generated_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (week_start, week_end) DO UPDATE SET
            total_calls           = EXCLUDED.total_calls,
            retail_calls          = EXCLUDED.retail_calls,
            trade_calls           = EXCLUDED.trade_calls,
            abandoned_total       = EXCLUDED.abandoned_total,
            retail_abandoned      = EXCLUDED.retail_abandoned,
            trade_abandoned       = EXCLUDED.trade_abandoned,
            report_generated_date = EXCLUDED.report_generated_date;
    """
    values = (
        _normalise_date(metrics["start_date"]),
        _normalise_date(metrics["end_date"]),
        metrics.get("total", 0),
        metrics.get("retail", 0),
        metrics.get("trade", 0),
        metrics.get("abandoned", 0),
        metrics.get("abandoned_retail", 0),
        metrics.get("abandoned_trade", 0),
        datetime.now(),
    )

    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(upsert_sql, values)
        print(
            f"Saved weekly stats for {metrics['start_date']} – {metrics['end_date']}."
        )
    except Exception:
        raise
    finally:
        if conn:
            conn.close()
