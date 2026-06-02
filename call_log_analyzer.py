"""Core call-log analysis engine.

Loads, concatenates, and deduplicates all ``CallLogLastWeek_*.csv`` files
from the data directory, then calculates weekly metrics, generates Plotly
charts, and exports cleaned datasets.  The public entry point is
:func:`analyze_calls`.

Pipeline stages inside ``analyze_calls``:

1. Clean and load every main call-log CSV via ``cleaning.run_cleaning``.
2. Optionally cap data to a ``target_max_date`` for historical back-fills.
3. Re-assign week numbers globally based on the combined max date.
4. Upsert deduplicated rows to the ``call_logs`` PostgreSQL table.
5. Load and classify abandoned calls.
6. Calculate weekly metrics (Retail / Trade / Abandoned splits).
7. Analyse caller journey and out-of-hours patterns.
8. Generate interactive Plotly charts.
9. Export cleaned CSVs to ``reports/``.
"""

import glob
import os
import re

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from psycopg2 import sql
from psycopg2.extras import execute_values

from cleaning import run_cleaning
from db import get_connection


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _ensure_call_id_constraint(cursor, table_name: str) -> None:
    """Add a UNIQUE constraint on ``"Call ID"`` if one does not exist.

    Checks for null and duplicate values first to avoid a failed ALTER TABLE.

    Args:
        cursor: An open psycopg2 cursor within an active transaction.
        table_name: Name of the target table.

    Raises:
        ValueError: If the table contains null or duplicate ``Call ID``
            values that would prevent adding the constraint.
    """
    table_ident = sql.Identifier(table_name)
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM   information_schema.table_constraints  tc
            JOIN   information_schema.key_column_usage   kcu
                   ON tc.constraint_name  = kcu.constraint_name
                  AND tc.constraint_schema = kcu.constraint_schema
            WHERE  tc.table_name      = %s
              AND  kcu.column_name    = 'Call ID'
              AND  tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
        );
        """,
        (table_name,),
    )
    if cursor.fetchone()[0]:
        return  # Constraint already exists.

    cursor.execute(
        sql.SQL('SELECT COUNT(*) FROM {} WHERE "Call ID" IS NULL;').format(table_ident)
    )
    null_count = cursor.fetchone()[0]
    if null_count:
        raise ValueError(
            f"Cannot add UNIQUE constraint to {table_name}: "
            f"{null_count} row(s) have a NULL Call ID."
        )

    cursor.execute(
        sql.SQL(
            'SELECT "Call ID", COUNT(*) FROM {} GROUP BY "Call ID" HAVING COUNT(*) > 1 LIMIT 5;'
        ).format(table_ident)
    )
    duplicates = cursor.fetchall()
    if duplicates:
        sample = ", ".join(str(r[0]) for r in duplicates)
        raise ValueError(
            f"Cannot add UNIQUE constraint to {table_name}: duplicate Call IDs found. "
            f"Examples: {sample}"
        )

    constraint_name = f"{table_name}_call_id_unique"
    cursor.execute(
        sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} UNIQUE (\"Call ID\");").format(
            table_ident, sql.Identifier(constraint_name)
        )
    )


def save_to_database(df: pd.DataFrame, table_name: str = "call_logs") -> None:
    """Upsert a call-log DataFrame into PostgreSQL, preserving historical rows.

    Creates the target table if it does not exist, then uses
    ``ON CONFLICT ("Call ID") DO UPDATE`` so that re-running the pipeline
    updates existing records rather than inserting duplicates.

    Args:
        df: Call-level DataFrame produced by :func:`analyze_calls`.  Must
            contain the columns listed in ``db_cols`` below.
        table_name: Target PostgreSQL table.  Defaults to ``"call_logs"``.
    """
    try:
        print(f"Saving {len(df)} rows to '{table_name}'...")
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                "Call ID"             TEXT PRIMARY KEY,
                call_start            TIMESTAMP,
                from_number           TEXT,
                to_number             TEXT,
                directions            TEXT,
                statuses              TEXT,
                ringing_total_sec     INTEGER,
                talking_total_sec     INTEGER,
                customer_type         TEXT,
                call_activity_details TEXT,
                is_answered           BOOLEAN,
                is_abandoned          BOOLEAN,
                date                  DATE,
                day_name              TEXT,
                week                  INTEGER,
                week_start            DATE
            );
            """
        )
        _ensure_call_id_constraint(cursor, table_name)

        db_cols = [
            "Call ID", "call_start", "from_number", "to_number",
            "directions", "statuses", "ringing_total_sec", "talking_total_sec",
            "customer_type", "call_activity_details", "is_answered", "is_abandoned",
            "date", "day_name", "week", "week_start",
        ]
        quoted_cols = [f'"{c}"' for c in db_cols]
        values = [tuple(x) for x in df[db_cols].to_numpy()]
        update_set = ", ".join(
            f"{q} = EXCLUDED.{q}" for q in quoted_cols if q != '"Call ID"'
        )
        insert_sql = (
            f"INSERT INTO {table_name} ({', '.join(quoted_cols)}) VALUES %s "
            f"ON CONFLICT (\"Call ID\") DO UPDATE SET {update_set}"
        )
        execute_values(cursor, insert_sql, values)
        conn.commit()
        print(f"Saved {len(df)} rows to '{table_name}'.")

    except Exception as exc:
        print(f"Error saving to database: {exc}")
        if "conn" in locals():
            conn.rollback()
    finally:
        if "cursor" in locals():
            cursor.close()
        if "conn" in locals():
            conn.close()


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def calculate_mode(series: pd.Series):
    """Return the most frequent value in a Series, or ``0`` if it is empty.

    Args:
        series: Any pandas Series.

    Returns:
        The mode value, or ``0`` when the Series is empty.
    """
    mode = series.mode()
    return mode.iloc[0] if not mode.empty else 0


def get_week_date_label(week_num: int, max_date: pd.Timestamp) -> str:
    """Format the start date of a week as ``DD/MM/YYYY`` for chart annotations.

    Week 1 ends on ``max_date``; Week 2 ends 7 days before that; and so on.

    Args:
        week_num: Week number (1 = most recent, 2 = previous, etc.).
        max_date: The global maximum call timestamp used as the week anchor.

    Returns:
        Start date of the requested week in ``DD/MM/YYYY`` format.
    """
    if not isinstance(max_date, pd.Timestamp):
        max_date = pd.to_datetime(max_date)
    days_back = (week_num - 1) * 7
    week_end = max_date - pd.Timedelta(days=days_back)
    week_start = week_end - pd.Timedelta(days=6)
    return week_start.strftime("%d/%m/%Y")


def clean_phone_for_match(phone) -> str:
    """Normalise a phone number string for matching against trade number sets.

    Strips whitespace, punctuation, and common Irish country-code prefixes
    (``+353``, ``00353``, ``353``) and removes a leading ``0``.

    Args:
        phone: Raw phone number value (string or anything coercible to str).

    Returns:
        Normalised digit-only string suitable for set membership tests.
    """
    s = str(phone).strip()
    for char in (" ", "-", ".", "(", ")"):
        s = s.replace(char, "")
    for prefix in ("+353", "00353", "353"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if s.startswith("0"):
        s = s[1:]
    return s


# ---------------------------------------------------------------------------
# Plot generation
# ---------------------------------------------------------------------------


def _format_time_hover(seconds) -> str:
    """Format a duration in seconds as a human-readable string for hover text.

    Args:
        seconds: Duration in seconds (numeric or NaN).

    Returns:
        A string like ``"4m 32s"`` or ``"45s"``, or ``"0s"`` for NaN.
    """
    if pd.isna(seconds):
        return "0s"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


def generate_plots(df: pd.DataFrame, abandoned_df: pd.DataFrame) -> tuple[dict, dict]:
    """Build the combined three-panel Plotly chart and derive plot-level metrics.

    The chart contains:

    * **Panel 1** – Average waiting time by customer type and week.
    * **Panel 2** – Average talking time by customer type and week.
    * **Panel 3** – Abandoned calls by day of week.

    Args:
        df: Full (non-filtered) call-level DataFrame including week assignments.
        abandoned_df: Full abandoned-calls DataFrame including week assignments.

    Returns:
        A dict with key ``"combined_plot"`` containing an HTML fragment.
    """
    color_map = {1: "#2391DC", 2: "#DC6E23"}
    days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    def _week_label(week: int) -> str:
        return "This Week" if week == 1 else "Last Week" if week == 2 else f"Week {week}"

    # --- Chart data preparation ---
    df_recent = df[df["week"].isin([1, 2])].copy()
    df_recent = df_recent[df_recent["customer_type"].isin(["retail", "trade"])]
    df_recent["customer_type_display"] = df_recent["customer_type"].map(
        {"retail": "Retail", "trade": "Trade Customer"}
    )

    grouped = (
        df_recent.groupby(["week", "customer_type_display"])
        .agg(
            avg_wait_time=("ringing_total_sec", "mean"),
            avg_talk_time=("talking_total_sec", "mean"),
            call_count=("ringing_total_sec", "count"),
        )
        .reset_index()
    )

    max_date = df["call_start"].max() if "call_start" in df.columns else pd.Timestamp.now()
    customer_types = ["Retail", "Trade Customer"]

    # ---- Chart 1: Average Waiting Time ----
    fig_waiting = go.Figure()
    for week in [1, 2]:
        week_data = grouped[grouped["week"] == week]
        y_vals, call_counts, hover_texts = [], [], []

        for ctype in customer_types:
            row = week_data[week_data["customer_type_display"] == ctype]
            if not row.empty:
                val = row["avg_wait_time"].values[0]
                count = int(row["call_count"].values[0])
                y_vals.append(val)
                call_counts.append(count)
                hover_texts.append(_format_time_hover(val))
            else:
                y_vals.append(0)
                call_counts.append(0)
                hover_texts.append("0s")

        label = _week_label(week)
        fig_waiting.add_trace(
            go.Bar(
                name=label,
                x=customer_types,
                y=y_vals,
                marker_color=color_map[week],
                text=[label] * len(customer_types),
                textposition="auto",
                customdata=np.array(list(zip(call_counts, hover_texts))),
                hovertemplate=(
                    "Type: %{x}<br>"
                    f"{label}<br>"
                    "Avg Waiting Time: %{customdata[1]}<br>"
                    "Total Calls: %{customdata[0]}<extra></extra>"
                ),
            )
        )

    # ---- Chart 2: Average Talking Time ----
    fig_talk = go.Figure()
    for week in [1, 2]:
        week_data = grouped[grouped["week"] == week]
        y_vals, call_counts, hover_texts = [], [], []

        for ctype in customer_types:
            row = week_data[week_data["customer_type_display"] == ctype]
            if not row.empty:
                val = row["avg_talk_time"].values[0]
                count = int(row["call_count"].values[0])
                y_vals.append(val / 60)
                call_counts.append(count)
                hover_texts.append(_format_time_hover(val))
            else:
                y_vals.append(0)
                call_counts.append(0)
                hover_texts.append("0s")

        label = _week_label(week)
        fig_talk.add_trace(
            go.Bar(
                name=label,
                x=customer_types,
                y=y_vals,
                marker_color=color_map[week],
                text=[label] * len(customer_types),
                textposition="auto",
                customdata=np.array(list(zip(call_counts, hover_texts))),
                hovertemplate=(
                    "Type: %{x}<br>"
                    f"{label}<br>"
                    "Avg Talk Time: %{customdata[1]}<br>"
                    "Total Calls: %{customdata[0]}<extra></extra>"
                ),
            )
        )

    # ---- Chart 3: Abandoned Calls by Day of Week ----
    fig_abandoned = go.Figure()
    week1_abd_data = pd.DataFrame()
    week2_abd_data = pd.DataFrame()
    all_abd_data = pd.DataFrame()

    if not abandoned_df.empty:
        abd = abandoned_df.copy()
        abd["Call Time"] = pd.to_datetime(abd["Call Time"], errors="coerce")

        if "week" not in abd.columns:
            print("WARNING: 'week' column missing in abandoned_df — skipping abandoned chart.")
            return {}

        abd = abd[abd["week"].isin([1, 2])].copy()
        abd["day_of_week"] = abd["Call Time"].dt.day_name()

        def _parse_wait(x) -> int:
            try:
                if pd.isna(x):
                    return 0
                parts = str(x).split(":")
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]) if len(parts) == 3 else 0
            except Exception:
                return 0

        abd["wait_sec"] = abd["Waiting Time"].apply(_parse_wait)
        main_df = df[df["week"].isin([1, 2])].copy()
        main_df["day_of_week"] = main_df["call_start"].dt.day_name()

        for week in [1, 2]:
            week_main = main_df[main_df["week"] == week]
            week_abd = abd[abd["week"] == week]

            y_vals = []
            custom_data = []
            for day in days_order:
                day_abd = week_abd[week_abd["day_of_week"] == day]
                abd_count = len(day_abd)
                y_vals.append(abd_count)

                min_w = day_abd["wait_sec"].min() if not day_abd.empty else 0
                avg_w = day_abd["wait_sec"].mean() if not day_abd.empty else 0
                max_w = day_abd["wait_sec"].max() if not day_abd.empty else 0

                day_main = week_main[week_main["day_of_week"] == day]
                main_count = len(day_main)
                answered = int(day_main["is_answered"].sum())

                custom_data.append([
                    main_count + abd_count,          # 0 – total calls
                    answered,                         # 1 – answered
                    abd_count,                        # 2 – abandoned
                    _format_time_hover(min_w),        # 3
                    _format_time_hover(avg_w),        # 4
                    _format_time_hover(max_w),        # 5
                    main_count - answered,            # 6 – voicemail/other
                ])

            label = _week_label(week)
            fig_abandoned.add_trace(
                go.Bar(
                    name=label,
                    x=days_order,
                    y=y_vals,
                    marker_color=color_map[week],
                    customdata=custom_data,
                    hovertemplate=(
                        "Day: %{x}<br>"
                        f"{label}<br>"
                        "Abandoned: %{y}<br>"
                        "Answered: %{customdata[1]}<br>"
                        "Voicemail/Other: %{customdata[6]}<br>"
                        "Total: %{customdata[0]}<br>"
                        "Min Wait: %{customdata[3]}<br>"
                        "Avg Wait: %{customdata[4]}<br>"
                        "Max Wait: %{customdata[5]}<extra></extra>"
                    ),
                )
            )

            if week == 1:
                week1_abd_data = week_abd
            else:
                week2_abd_data = week_abd
            all_abd_data = pd.concat([all_abd_data, week_abd])

    # ---- Combine into a single figure ----
    combined_fig = make_subplots(
        rows=3,
        cols=1,
        subplot_titles=("Average Waiting Time", "Average Talking Time", "Abandoned Calls by Day of Week"),
        vertical_spacing=0.08,
        row_heights=[0.33, 0.33, 0.33],
    )

    for trace in fig_waiting.data:
        trace.showlegend = False
        combined_fig.add_trace(trace, row=1, col=1)
    for trace in fig_talk.data:
        trace.showlegend = False
        combined_fig.add_trace(trace, row=2, col=1)
    for trace in fig_abandoned.data:
        trace.showlegend = True
        combined_fig.add_trace(trace, row=3, col=1)

    # ---- Annotation on abandoned panel ----
    if not abandoned_df.empty and not all_abd_data.empty:
        w1_mean = week1_abd_data["wait_sec"].mean() if not week1_abd_data.empty else 0
        w2_mean = week2_abd_data["wait_sec"].mean() if not week2_abd_data.empty else 0
        w1_count = len(week1_abd_data)
        w2_count = len(week2_abd_data)

        max_y = all_abd_data.groupby(["week", "day_of_week"]).size().max() if not all_abd_data.empty else 10

        combined_fig.add_annotation(
            text=(
                f"<b>Avg Time to Abandon:</b><br>"
                f"Week {get_week_date_label(1, max_date)}: {_format_time_hover(w1_mean)} ({w1_count} calls)<br>"
                f"Week {get_week_date_label(2, max_date)}: {_format_time_hover(w2_mean)} ({w2_count} calls)"
            ),
            xref="x3", yref="y3",
            x=-0.4, y=max_y * 0.8,
            showarrow=False,
            align="left",
            xanchor="left",
            yanchor="middle",
            bgcolor="rgba(255,255,255,0.8)",
            bordercolor="black",
            borderwidth=1,
            font=dict(size=10),
        )

    combined_fig.update_yaxes(title_text="Seconds", row=1, col=1)
    combined_fig.update_yaxes(title_text="Minutes", row=2, col=1)
    combined_fig.update_yaxes(title_text="Count", row=3, col=1)
    combined_fig.update_layout(
        title_text="Southside Call Center Metrics",
        title_x=0.5,
        showlegend=True,
        barmode="group",
        height=1200,
        autosize=True,
        legend=dict(orientation="h", yanchor="top", y=-0.05, xanchor="center", x=0.5),
        margin=dict(l=20, r=20, t=60, b=50),
    )

    plots = {
        "combined_plot": combined_fig.to_html(
            full_html=False,
            include_plotlyjs="cdn",
            config={"responsive": True, "displayModeBar": False},
        )
    }
    return plots


# ---------------------------------------------------------------------------
# Abandoned call loading and classification
# ---------------------------------------------------------------------------


def load_abandoned_calls(data_dir: str = "data") -> pd.DataFrame:
    """Load, deduplicate, and combine all ``AbandonedCalls*.csv`` files.

    Caller IDs are normalised (trailing ``.0`` removed) immediately after
    loading.  Deduplication is performed on ``(Caller ID, Call Time)`` to
    handle files that overlap across weekly exports.

    Args:
        data_dir: Directory to search for abandoned-call CSV files.

    Returns:
        Combined DataFrame, or an empty DataFrame if no files are found.
    """
    files = glob.glob(os.path.join(data_dir, "AbandonedCalls*.csv"))
    dfs = []
    for f in files:
        try:
            dfs.append(pd.read_csv(f))
        except Exception as exc:
            print(f"Error reading {f}: {exc}")

    if not dfs:
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)

    if "Caller ID" in combined.columns:
        combined["Caller ID"] = combined["Caller ID"].astype(str).str.replace(
            r"\.0$", "", regex=True
        )

    before = len(combined)
    combined = combined.drop_duplicates(subset=["Caller ID", "Call Time"])
    print(f"Deduplicated abandoned logs: {before} -> {len(combined)}")

    output_path = os.path.join(data_dir, "combined_abandoned_call_logs.csv")
    combined.to_csv(output_path, index=False)
    print(f"Saved combined abandoned calls to {output_path}")
    return combined


# ---------------------------------------------------------------------------
# Journey analysis
# ---------------------------------------------------------------------------


def analyze_journey(main_df: pd.DataFrame, abandoned_df: pd.DataFrame) -> dict:
    """Analyse call routing paths through Queues, Voicemail, OOH, and termination.

    For main log calls: counts queue usage, voicemail routing, out-of-office
    escalations, and whether calls were ended by the agent, customer, or
    system.

    For abandoned calls: counts how many occurred when agents were logged out
    and breaks those down by time-of-day (before/during/after business hours).
    Also counts calls with zero polling attempts.

    Args:
        main_df: Call-level DataFrame for Weeks 1 and 2 (from
            :func:`analyze_calls`).
        abandoned_df: Abandoned-call DataFrame for Weeks 1 and 2.

    Returns:
        A flat dict of journey metric counts suitable for merging into the
        main metrics dict.
    """
    stats: dict = {}

    main_df = main_df.copy()
    main_df["call_activity_details"] = main_df.get("call_activity_details", pd.Series(dtype=str)).fillna("")

    # Queue / OOH / Voicemail
    details = main_df["call_activity_details"]
    stats["queue_calls"] = int(details.str.contains("Queue", case=False).sum())
    stats["ooo_calls"] = int(details.str.contains("Out of office", case=False).sum())
    stats["voicemail_calls"] = int(details.str.contains("Voice Agent", case=False).sum())

    # Termination type
    def _terminator(text: str) -> str:
        if "Ended by Voice Agent" in text:
            return "System"
        m = re.search(r"Ended by ([^:|]+)", text)
        if m:
            t = m.group(1).strip()
            return "Customer" if any(c.isdigit() for c in t) and len(t) > 5 else "Agent"
        return "Unknown"

    term_counts = main_df["call_activity_details"].apply(_terminator).value_counts()
    stats["ended_by_agent"] = int(term_counts.get("Agent", 0))
    stats["ended_by_customer"] = int(term_counts.get("Customer", 0))
    stats["ended_by_system"] = int(term_counts.get("System", 0))

    # Abandoned log analysis
    if not abandoned_df.empty and "Agent State" in abandoned_df.columns:
        logged_out_df = abandoned_df[abandoned_df["Agent State"] == "Logged Out"].copy()
        stats["abd_agent_logged_out"] = int(len(logged_out_df))
        stats["abd_agent_logged_in"] = int(
            (abandoned_df["Agent State"] == "Logged In").sum()
        )

        if not logged_out_df.empty and "Call Time" in logged_out_df.columns:
            logged_out_df["Call Time"] = pd.to_datetime(
                logged_out_df["Call Time"], errors="coerce"
            )
            logged_out_df = logged_out_df.dropna(subset=["Call Time"])
            hour = logged_out_df["Call Time"].dt.hour
            day = logged_out_df["Call Time"].dt.dayofweek  # 0=Mon … 6=Sun

            mon_fri = day <= 4
            sat = day == 5
            sun = day == 6

            before = (mon_fri & (hour < 8)) | (sat & (hour < 8)) | (sun & (hour < 10))
            after = (mon_fri & (hour >= 20)) | (sat & (hour >= 18)) | (sun & (hour >= 16))
            during = ~(before | after)

            stats["abd_logged_out_before_hours"] = int(before.sum())
            stats["abd_logged_out_after_hours"] = int(after.sum())
            stats["abd_logged_out_during_hours"] = int(during.sum())
        else:
            stats.update({
                "abd_logged_out_before_hours": 0,
                "abd_logged_out_after_hours": 0,
                "abd_logged_out_during_hours": 0,
            })
    else:
        stats.update({
            "abd_agent_logged_out": 0,
            "abd_agent_logged_in": 0,
            "abd_logged_out_before_hours": 0,
            "abd_logged_out_after_hours": 0,
            "abd_logged_out_during_hours": 0,
        })

    if not abandoned_df.empty and "Polling Attempts" in abandoned_df.columns:
        stats["abd_zero_polling"] = int((abandoned_df["Polling Attempts"] == 0).sum())
    else:
        stats["abd_zero_polling"] = 0

    return stats


# ---------------------------------------------------------------------------
# Out-of-hours analysis
# ---------------------------------------------------------------------------


def analyze_out_of_hours(main_df: pd.DataFrame, abandoned_df: pd.DataFrame) -> dict:
    """Count calls received outside published operating hours.

    Operating hours:

    * Mon–Fri: 08:00–20:00
    * Saturday: 08:00–18:00
    * Sunday: 10:00–16:00

    Args:
        main_df: Filtered call-level DataFrame (Weeks 1 and 2 only).
        abandoned_df: Filtered abandoned-call DataFrame (Weeks 1 and 2 only).

    Returns:
        A dict with keys ``ooh_total``, ``ooh_before_opening``, and
        ``ooh_after_closing``.
    """
    frames = []
    if not main_df.empty:
        frames.append(main_df[["call_start"]].rename(columns={"call_start": "ts"}))
    if not abandoned_df.empty:
        frames.append(
            abandoned_df[["Call Time"]].rename(columns={"Call Time": "ts"})
        )

    if not frames:
        return {"ooh_total": 0, "ooh_before_opening": 0, "ooh_after_closing": 0}

    combined = pd.concat(frames, ignore_index=True)
    combined["ts"] = pd.to_datetime(combined["ts"], errors="coerce")
    combined = combined.dropna(subset=["ts"])

    hour = combined["ts"].dt.hour
    day = combined["ts"].dt.dayofweek  # 0=Mon … 6=Sun

    mon_fri = day <= 4
    sat = day == 5
    sun = day == 6

    before = (mon_fri & (hour < 8)) | (sat & (hour < 8)) | (sun & (hour < 10))
    after = (mon_fri & (hour >= 20)) | (sat & (hour >= 18)) | (sun & (hour >= 16))
    ooh = before | after

    return {
        "ooh_total": int(ooh.sum()),
        "ooh_before_opening": int(before[ooh].sum()),
        "ooh_after_closing": int(after[ooh].sum()),
    }


# ---------------------------------------------------------------------------
# Main analysis entry point
# ---------------------------------------------------------------------------


def analyze_calls(data_dir: str = "data", target_max_date: str | None = None) -> dict:
    """Run the full call-log analysis pipeline.

    Loads every ``CallLogLastWeek_*.csv`` in ``data_dir``, cleans and
    deduplicates the records, re-assigns week numbers globally, classifies
    abandoned calls, calculates metrics, generates charts, and exports
    cleaned CSVs.

    Args:
        data_dir: Directory containing the call-log CSV files.  Defaults to
            ``"data"``.
        target_max_date: Optional ceiling date in ``YYYY-MM-DD`` format.
            When supplied, records after this date are excluded before week
            assignment.  Used by ``generate_report.py --date`` for
            historical back-fills.

    Returns:
        A dict with keys:

        * ``metrics`` – flat dict of all calculated metrics.
        * ``plots`` – dict with ``"combined_plot"`` HTML fragment.
        * ``narrative`` – empty string (narrative is built in
          ``generate_report.py`` after historical overrides).
        * ``raw_data`` – call-level DataFrame for Weeks 1 and 2.
        * ``abandoned_logs`` – abandoned DataFrame for Weeks 1 and 2.
        * ``raw_data_all_weeks`` – full call-level DataFrame (all weeks).
        * ``abandoned_all_weeks`` – full abandoned DataFrame.
        * ``max_date`` – ``DD/MM/YYYY`` string of the global max date.
        * ``max_date_obj`` – the max date as a ``pd.Timestamp``.
        * ``abandoned_trade_customers`` – dict ``{week1: [...], week2: [...]}``.

        Returns an empty dict if no main log files are found.
    """
    # ---- Stage 1: Load and clean main call logs -----------------------------
    print("Cleaning and loading main call logs...")
    files = glob.glob(os.path.join(data_dir, "CallLogLastWeek_*.csv"))
    dfs = []
    for f in files:
        print(f"  Processing {os.path.basename(f)}...")
        try:
            dfs.append(run_cleaning(f).call_level_df)
        except Exception as exc:
            print(f"  ERROR processing {f}: {exc}")

    if not dfs:
        print("No main call logs found.")
        return {}

    df = pd.concat(dfs, ignore_index=True).drop_duplicates(subset=["Call ID"])
    print(f"Total unique calls after deduplication: {len(df)}")

    # ---- Stage 2: Apply target date cap (historical back-fill) --------------
    if target_max_date is not None:
        cap = pd.to_datetime(target_max_date).normalize()
        cap_end = cap + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        df = df[df["call_start"] <= cap_end]
        print(f"Filtered to target max date: {cap.date()}")

    # ---- Stage 3: Re-assign weeks globally ----------------------------------
    max_date = df["call_start"].max()
    print(f"Global max date: {max_date}")

    week1_start = max_date - pd.Timedelta(days=7)
    week2_start = week1_start - pd.Timedelta(days=7)

    conditions = [
        (df["call_start"] > week1_start) & (df["call_start"] <= max_date),
        (df["call_start"] > week2_start) & (df["call_start"] <= week1_start),
    ]
    df["week"] = np.select(conditions, [1, 2], default=3)
    print(f"Week distribution: {df['week'].value_counts().sort_index().to_dict()}")

    # ---- Stage 4: Persist to database ---------------------------------------
    save_to_database(df)

    # ---- Stage 5: Load and classify abandoned calls -------------------------
    print("Loading abandoned call logs...")
    abandoned_df = load_abandoned_calls(data_dir)

    if not abandoned_df.empty:
        trade_numbers = set(
            df[df["customer_type"] == "trade"]["from_number"]
            .apply(clean_phone_for_match)
            .unique()
        ) - {"anonymous", ""}

        abandoned_df["customer_type"] = abandoned_df["Caller ID"].apply(
            lambda x: "trade" if clean_phone_for_match(x) in trade_numbers else "retail"
        )
        # Catch any residual 'unknown' values and default them to retail.
        if "customer_type" in abandoned_df.columns:
            mask = abandoned_df["customer_type"].str.lower() == "unknown"
            abandoned_df.loc[mask, "customer_type"] = "retail"

        abandoned_df["Call Time"] = pd.to_datetime(
            abandoned_df["Call Time"], errors="coerce"
        )

        if target_max_date is not None:
            abandoned_df = abandoned_df[abandoned_df["Call Time"] <= cap_end]

        # Assign weeks to abandoned calls using the same boundaries.
        cond_abd = [
            (abandoned_df["Call Time"] > week1_start) & (abandoned_df["Call Time"] <= max_date),
            (abandoned_df["Call Time"] > week2_start) & (abandoned_df["Call Time"] <= week1_start),
        ]
        abandoned_df["week"] = np.select(cond_abd, [1, 2], default=3)
        abandoned_df["week_start"] = (
            abandoned_df["Call Time"].dt.normalize()
            - pd.to_timedelta(abandoned_df["Call Time"].dt.dayofweek, unit="D")
        )
        print(
            f"Abandoned week distribution: "
            f"{abandoned_df['week'].value_counts().sort_index().to_dict()}"
        )

    # ---- Stage 6: Calculate metrics -----------------------------------------
    # Work exclusively with Weeks 1 and 2 for all metric calculations.
    df_week12 = df[df["week"].isin([1, 2])].copy()
    abandoned_week12 = (
        abandoned_df[abandoned_df["week"].isin([1, 2])].copy()
        if not abandoned_df.empty
        else pd.DataFrame()
    )

    # Normalise max_date to midnight so the inclusive range covers full days.
    max_date_norm = max_date.normalize()
    this_week_end = max_date_norm + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    this_week_start = max_date_norm - pd.Timedelta(days=6)
    last_week_end = this_week_start - pd.Timedelta(seconds=1)
    last_week_start = this_week_start - pd.Timedelta(days=7)

    print(
        f"\nThis Week: {this_week_start.date()} to {max_date_norm.date()}\n"
        f"Last Week: {last_week_start.date()} to {(this_week_start - pd.Timedelta(days=1)).date()}"
    )

    # Filter by date range (more precise than week number for boundary days).
    week1 = df_week12[
        (df_week12["call_start"] >= this_week_start)
        & (df_week12["call_start"] <= this_week_end)
    ].copy()
    week2 = df_week12[
        (df_week12["call_start"] >= last_week_start)
        & (df_week12["call_start"] <= last_week_end)
    ].copy()
    print(f"This Week main calls: {len(week1)}\nLast Week main calls: {len(week2)}")

    week1_retail = week1[week1["customer_type"] == "retail"]
    week1_trade = week1[week1["customer_type"] == "trade"]
    week2_retail = week2[week2["customer_type"] == "retail"]
    week2_trade = week2[week2["customer_type"] == "trade"]

    if not abandoned_week12.empty:
        week1_abd = abandoned_week12[
            (abandoned_week12["Call Time"] >= this_week_start)
            & (abandoned_week12["Call Time"] <= this_week_end)
        ].copy()
        week2_abd = abandoned_week12[
            (abandoned_week12["Call Time"] >= last_week_start)
            & (abandoned_week12["Call Time"] <= last_week_end)
        ].copy()
        print(f"This Week abandoned: {len(week1_abd)}\nLast Week abandoned: {len(week2_abd)}")

        w1_retail_abd = len(week1_abd[week1_abd["customer_type"] == "retail"])
        w1_trade_abd = len(week1_abd[week1_abd["customer_type"] == "trade"])
        w2_retail_abd = len(week2_abd[week2_abd["customer_type"] == "retail"])
        w2_trade_abd = len(week2_abd[week2_abd["customer_type"] == "trade"])
    else:
        w1_retail_abd = w1_trade_abd = w2_retail_abd = w2_trade_abd = 0

    total_retail_abd = w1_retail_abd + w2_retail_abd
    total_trade_abd = w1_trade_abd + w2_trade_abd

    week1_total = len(week1) + w1_retail_abd + w1_trade_abd
    week2_total = len(week2) + w2_retail_abd + w2_trade_abd

    total_retail_main = len(df_week12[df_week12["customer_type"] == "retail"])
    total_trade_main = len(df_week12[df_week12["customer_type"] == "trade"])
    total_abandoned = (w1_retail_abd + w1_trade_abd + w2_retail_abd + w2_trade_abd)
    total_calls = week1_total + week2_total
    answered = int(df_week12["is_answered"].sum())
    abandonment_rate = round(total_abandoned / total_calls * 100, 1) if total_calls else 0.0

    def _abd_rate(abd: int, main: int) -> float:
        return round(abd / (main + abd) * 100, 1) if (main + abd) else 0.0

    metrics: dict = {
        "total_calls": total_calls,
        "answered_calls": answered,
        "abandoned_calls": total_abandoned,
        "abandonment_rate": abandonment_rate,

        "this_week_start": this_week_start.strftime("%Y-%m-%d"),
        "this_week_end": this_week_end.strftime("%Y-%m-%d"),
        "last_week_start": last_week_start.strftime("%Y-%m-%d"),
        "last_week_end": last_week_end.strftime("%Y-%m-%d"),

        "week1_calls": week1_total,
        "week2_calls": week2_total,

        "week1_retail_calls": len(week1_retail),
        "week1_trade_calls": len(week1_trade),
        "week2_retail_calls": len(week2_retail),
        "week2_trade_calls": len(week2_trade),

        "week1_retail_abandoned": w1_retail_abd,
        "week1_trade_abandoned": w1_trade_abd,
        "week2_retail_abandoned": w2_retail_abd,
        "week2_trade_abandoned": w2_trade_abd,

        "week1_abandoned_total": w1_retail_abd + w1_trade_abd,
        "week2_abandoned_total": w2_retail_abd + w2_trade_abd,

        "retail_abandoned": total_retail_abd,
        "trade_abandoned": total_trade_abd,

        "week1_retail_total": len(week1_retail),
        "week1_trade_total": len(week1_trade),
        "week2_retail_total": len(week2_retail),
        "week2_trade_total": len(week2_trade),

        "retail_abandonment_rate": _abd_rate(total_retail_abd, total_retail_main),
        "trade_abandonment_rate": _abd_rate(total_trade_abd, total_trade_main),

        "week1_retail_abandonment_rate": _abd_rate(w1_retail_abd, len(week1_retail)),
        "week1_trade_abandonment_rate": _abd_rate(w1_trade_abd, len(week1_trade)),
        "week2_retail_abandonment_rate": _abd_rate(w2_retail_abd, len(week2_retail)),
        "week2_trade_abandonment_rate": _abd_rate(w2_trade_abd, len(week2_trade)),
    }

    # ---- Stage 7: Journey and OOH analysis ----------------------------------
    metrics.update(analyze_journey(df_week12, abandoned_week12))
    metrics.update(analyze_out_of_hours(df_week12, abandoned_week12))

    # ---- Abandoned trade customer list (for HTML table) ---------------------
    abandoned_trade_customers: dict = {"week1": [], "week2": []}
    if not abandoned_week12.empty:
        trade_names_path = os.path.join(data_dir, "trade_customer_numbers.csv")
        trade_names_map: dict = {}
        if os.path.exists(trade_names_path):
            try:
                tdf = pd.read_csv(trade_names_path)
                if {"phone_number", "customer_name"}.issubset(tdf.columns):
                    trade_names_map = {
                        clean_phone_for_match(r["phone_number"]): r["customer_name"]
                        for _, r in tdf.iterrows()
                    }
            except Exception as exc:
                print(f"Could not load trade customer names: {exc}")

        for week_num in [1, 2]:
            week_key = f"week{week_num}"
            week_abandoned = abandoned_df[
                (abandoned_df["week"] == week_num)
                & (abandoned_df["customer_type"] == "trade")
            ].copy()

            if week_abandoned.empty:
                continue

            week_abandoned["cleaned_phone"] = week_abandoned["Caller ID"].apply(
                clean_phone_for_match
            )
            records = []
            for _, row in week_abandoned.iterrows():
                name = trade_names_map.get(row["cleaned_phone"])
                if not name or name.strip().upper() == "UNKNOWN":
                    continue
                call_time = row["Call Time"]
                if pd.notnull(call_time):
                    if isinstance(call_time, str):
                        call_time = pd.to_datetime(call_time, errors="coerce")
                    time_str = call_time.strftime("%d/%m/%Y %H:%M") if pd.notnull(call_time) else "N/A"
                    sort_key = call_time if pd.notnull(call_time) else pd.Timestamp.min
                else:
                    time_str = "N/A"
                    sort_key = pd.Timestamp.min
                records.append({
                    "name": name.upper(),
                    "phone": row["Caller ID"],
                    "call_time": time_str,
                    "_sort": sort_key,
                })

            records.sort(key=lambda x: x["_sort"], reverse=True)
            for r in records:
                del r["_sort"]
            abandoned_trade_customers[week_key] = records

    # ---- Stage 8: Generate charts -------------------------------------------
    plots = generate_plots(df, abandoned_df)

    # ---- Stage 9: Export cleaned CSVs ---------------------------------------
    print("Exporting cleaned datasets...")
    os.makedirs("reports", exist_ok=True)
    try:
        df.to_csv("reports/call_logs_cleaned.csv", index=False)

        raw_dfs = [pd.read_csv(f) for f in files]
        pd.concat(raw_dfs, ignore_index=True).to_csv(
            "reports/call_logs_original.csv", index=False
        )

        if not abandoned_df.empty:
            abandoned_df.to_csv("reports/abandoned_logs_cleaned.csv", index=False)

        abd_files = glob.glob(os.path.join(data_dir, "AbandonedCalls*.csv"))
        if abd_files:
            abd_raw = [pd.read_csv(f) for f in abd_files]
            pd.concat(abd_raw, ignore_index=True).to_csv(
                "reports/abandoned_logs_original.csv", index=False
            )
    except Exception as exc:
        print(f"Error exporting datasets: {exc}")

    return {
        "metrics": metrics,
        "plots": plots,
        "narrative": "",  # Built by generate_report.py after historical overrides.
        "raw_data": df_week12,
        "abandoned_logs": (abandoned_week12 if not abandoned_week12.empty else pd.DataFrame()),
        "raw_data_all_weeks": df,
        "abandoned_all_weeks": abandoned_df,
        "max_date": max_date.strftime("%d/%m/%Y") if pd.notnull(max_date) else "N/A",
        "max_date_obj": max_date if pd.notnull(max_date) else None,
        "abandoned_trade_customers": abandoned_trade_customers,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    results = analyze_calls("data")
    print("Analysis complete.")
