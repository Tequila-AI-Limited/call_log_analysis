"""Call log cleaning and aggregation pipeline.

Converts raw 3CX call-leg CSV files into a single, deduplicated, call-level
DataFrame ready for metric calculation.  The public entry point is
``run_cleaning``; everything else is a helper used by that function.

Typical usage::

    from cleaning import run_cleaning
    result = run_cleaning("data/CallLogLastWeek_2026-05-10.csv")
    df = result.call_level_df
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_hms_to_seconds(value: str) -> int:
    """Convert a ``HH:MM:SS`` duration string to total seconds.

    Args:
        value: A duration string in ``HH:MM:SS`` format.  Any value that
            cannot be parsed is treated as zero rather than raising.

    Returns:
        Total duration in seconds, or ``0`` for unparsable input.
    """
    try:
        parts = str(value).split(":")
        if len(parts) != 3:
            return 0
        h, m, sec = map(int, parts)
        return h * 3600 + m * 60 + sec
    except Exception:
        return 0


def classify_customer_from_activity(activity: str) -> str | None:
    """Infer customer type from the ``Call Activity Details`` text of a single leg.

    The heuristic is: look for an ``Inbound: <token>`` pattern.  If the first
    character of ``<token>`` is a digit the caller dialled a retail number;
    otherwise they dialled a trade number.

    Args:
        activity: Raw text from the ``Call Activity Details`` column.

    Returns:
        ``"retail"``, ``"trade"``, or ``None`` if the pattern is absent.
    """
    if not isinstance(activity, str):
        return None

    m = re.search(r"Inbound:\s*(.+?)(?:\s*→|\s*\(|$)", activity)
    if not m:
        return None

    token = m.group(1).strip()
    if not token:
        return None

    return "retail" if token[0].isdigit() else "trade"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class CleanedData:
    """Container returned by ``run_cleaning``.

    Attributes:
        raw_call_df: Leg-level rows after initial cleaning (one row per CSV
            line that survived filtering).
        call_level_df: One row per unique Call ID after aggregation, with
            computed columns such as ``week``, ``customer_type``, and
            duration totals.
    """

    raw_call_df: pd.DataFrame
    call_level_df: pd.DataFrame


# ---------------------------------------------------------------------------
# Stage 1 – load and clean raw legs
# ---------------------------------------------------------------------------


def clean_call_log(call_log_path: str) -> pd.DataFrame:
    """Load a raw call-log CSV and clean it to leg level.

    Steps performed:

    1. Drop the ``Totals`` summary row (identified by a non-datetime
       ``Call Time``).
    2. Convert ``Ringing`` and ``Talking`` duration strings to seconds.
    3. Classify each leg as ``"retail"``, ``"trade"``, or ``None``.
    4. Keep only inbound legs (``Direction`` in ``Inbound``,
       ``Inbound Queue``).

    Args:
        call_log_path: Absolute or relative path to a
            ``CallLogLastWeek_*.csv`` file.

    Returns:
        Cleaned, inbound-only leg-level DataFrame.
    """
    df = pd.read_csv(call_log_path)

    df["Call Time dt"] = pd.to_datetime(df["Call Time"], errors="coerce")
    df = df[~df["Call Time dt"].isna()].copy()

    df["Ringing_sec"] = df["Ringing"].apply(parse_hms_to_seconds)
    df["Talking_sec"] = df["Talking"].apply(parse_hms_to_seconds)

    df["customer_type_leg"] = df["Call Activity Details"].apply(
        classify_customer_from_activity
    )

    inbound_mask = df["Direction"].isin(["Inbound", "Inbound Queue"])
    return df[inbound_mask].copy()


# ---------------------------------------------------------------------------
# Stage 2 – aggregate legs to call level
# ---------------------------------------------------------------------------


def _resolve_customer_type(series: pd.Series) -> str:
    """Choose the definitive customer type from all legs of a single call.

    ``"trade"`` takes priority over ``"retail"`` because a single trade leg
    is conclusive; ambiguous calls (no typed leg) default to ``"retail"``.

    Args:
        series: Series of per-leg customer type strings (may contain ``None``).

    Returns:
        ``"trade"``, ``"retail"``, or ``"retail"`` for unknown cases.
    """
    vals = {v for v in series if isinstance(v, str)}
    if "trade" in vals:
        return "trade"
    return "retail"


def aggregate_to_call_level(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse leg-level rows into one row per Call ID.

    Aggregation rules:

    * ``call_start`` – earliest leg timestamp.
    * Duration columns – summed across all legs.
    * ``customer_type`` – resolved by :func:`_resolve_customer_type`.
    * ``is_answered`` – ``True`` if ``talking_total_sec > 0``.
    * ``is_abandoned`` – ``True`` if ringing > 0 but talking == 0.
    * ``week`` – assigned by comparing ``call_start`` against the file's own
      ``max_date`` (a global override is applied later in
      ``call_log_analyzer.analyze_calls``).

    Args:
        df: Leg-level DataFrame produced by :func:`clean_call_log`.

    Returns:
        Call-level DataFrame with one row per unique ``Call ID``.
    """
    grouped = (
        df.groupby("Call ID")
        .agg(
            call_start=("Call Time dt", "min"),
            from_number=("From", "first"),
            to_number=("To", "first"),
            directions=("Direction", lambda x: ",".join(sorted(set(x)))),
            statuses=("Status", lambda x: ",".join(sorted(set(x)))),
            ringing_total_sec=("Ringing_sec", "sum"),
            talking_total_sec=("Talking_sec", "sum"),
            customer_type=("customer_type_leg", _resolve_customer_type),
            call_activity_details=(
                "Call Activity Details",
                lambda x: " | ".join(sorted(set(x.dropna().astype(str)))),
            ),
        )
        .reset_index()
    )

    grouped["is_answered"] = grouped["talking_total_sec"] > 0
    grouped["is_abandoned"] = (grouped["talking_total_sec"] == 0) & (
        grouped["ringing_total_sec"] > 0
    )
    grouped["date"] = grouped["call_start"].dt.date
    grouped["day_name"] = grouped["call_start"].dt.day_name()

    # Assign weeks relative to this file's max date.
    # call_log_analyzer.analyze_calls re-assigns weeks globally once all
    # files are concatenated, so this is only an interim label.
    max_date = grouped["call_start"].max()
    week1_start = max_date - pd.Timedelta(days=7)
    week2_start = week1_start - pd.Timedelta(days=7)

    conditions = [
        (grouped["call_start"] > week1_start) & (grouped["call_start"] <= max_date),
        (grouped["call_start"] > week2_start) & (grouped["call_start"] <= week1_start),
    ]
    grouped["week"] = np.select(conditions, [1, 2], default=3)

    grouped["week_start"] = (
        grouped["call_start"].dt.normalize()
        - pd.to_timedelta(grouped["call_start"].dt.dayofweek, unit="D")
    )

    return grouped


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_cleaning(call_log_path: str) -> CleanedData:
    """Run the full cleaning pipeline for a single call-log CSV.

    This is the only function that external modules need to call.  It chains
    :func:`clean_call_log` and :func:`aggregate_to_call_level` and returns
    both the raw leg frame and the aggregated call frame.

    Args:
        call_log_path: Path to a ``CallLogLastWeek_*.csv`` file.

    Returns:
        A :class:`CleanedData` instance containing ``raw_call_df`` (leg
        level) and ``call_level_df`` (call level).
    """
    raw_call_df = clean_call_log(call_log_path)
    call_level_df = aggregate_to_call_level(raw_call_df)
    return CleanedData(raw_call_df=raw_call_df, call_level_df=call_level_df)
