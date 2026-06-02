"""
Call Analytics Utility Functions

This module provides utility functions for processing and visualizing call center data.
It includes functions for data cleaning, transformation, and creating interactive plots
using Plotly.

Main components:
- Data conversion utilities (time formatting, name extraction)
- Week labeling and date range calculations
- Visualization functions for call metrics
- Color mapping for consistent chart styling
"""

import pandas as pd
import numpy as np
import re
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.express as px


# =============================================================================
# CONFIGURATION
# =============================================================================

# Week color mapping - customize these colors as needed
WEEK_COLORS = {
    1: "blue",   # Most recent week
    2: "red",    # Previous week
    3: "gray"    # Older data
}

# Customer type normalization mapping
CUSTOMER_TYPE_MAPPING = {
    "trade customers": "Trade Customer",
    "trade customer": "Trade Customer",
    "trade": "Trade Customer",
    "Retail": "Retail",
    "retail": "Retail",
}


# =============================================================================
# DATA CONVERSION UTILITIES
# =============================================================================

def hms_to_seconds(time_value):
    """
    Convert time in HH:MM:SS or MM:SS format to total seconds.

    Args:
        time_value: Time as string (HH:MM:SS or MM:SS) or numeric value

    Returns:
        float: Total seconds, or None if conversion fails

    Examples:
        >>> hms_to_seconds("1:30:45")
        5445
        >>> hms_to_seconds("5:30")
        330
        >>> hms_to_seconds(120)
        120
    """
    try:
        if pd.isna(time_value):
            return None

        # If already numeric, return as is
        if isinstance(time_value, (int, float)):
            return time_value

        # Parse string format
        parts = str(time_value).split(":")

        if len(parts) == 3:  # HH:MM:SS
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
        elif len(parts) == 2:  # MM:SS
            minutes, seconds = parts
            return int(minutes) * 60 + int(seconds)
        else:
            return float(time_value)

    except Exception:
        return None



def extract_phone_number(caller_id):
    """
    Extract phone number from various Caller ID formats.

    Handles formats like:
    - Plain number: "0851234567"
    - Parentheses: "Name (0851234567)"
    - Colon separator: "Name:0851234567"

    Args:
        caller_id: Caller ID string in various formats

    Returns:
        str: Phone number or None if not found
    """
    if pd.isna(caller_id):
        return None

    caller_str = str(caller_id).strip()

    # Already a plain phone number
    if re.match(r'^\d+$', caller_str):
        return caller_str

    # Extract from parentheses: "NAME (0123456789)"
    match = re.search(r'\((\d+)\)', caller_str)
    if match:
        return match.group(1)

    # Extract after colon: "NAME:0123456789"
    match = re.search(r':(\d+)', caller_str)
    if match:
        return match.group(1)

    return None


# =============================================================================
# DATE AND WEEK UTILITIES
# =============================================================================

def get_week_date_label(week_num, reference_date=None):
    """
    Convert week number to Monday date label.

    Week 1 = most recent complete Monday-Sunday week (based on max date in dataset)
    Week 2 = Monday 7 days before that, etc.

    If reference_date is a Monday, Week 1 is the previous complete week.

    Args:
        week_num (int): Week number (1, 2, 3, ...)
        reference_date (pd.Timestamp, optional): Reference date (typically max date from dataset)
                                                 Defaults to current date.

    Returns:
        str: Formatted Monday date string (DD/MM/YYYY)
    """
    if reference_date is None:
        reference_date = pd.Timestamp.now().normalize()

    # Find the Monday at or before the reference date (0 = Monday, 6 = Sunday)
    days_since_monday = reference_date.weekday()
    most_recent_monday = reference_date - pd.Timedelta(days=days_since_monday)

    # If reference_date is a Monday, we want the PREVIOUS complete week as Week 1
    if days_since_monday == 0:
        # Reference date is Monday - Week 1 starts 7 days before
        week1_monday = most_recent_monday - pd.Timedelta(days=7)
    else:
        # Reference date is Tue-Sun - Week 1 is the week containing reference_date
        week1_monday = most_recent_monday

    # Calculate the Monday for the requested week
    days_back = 7 * (week_num - 1)
    target_monday = week1_monday - pd.Timedelta(days=days_back)

    return target_monday.strftime("%d/%m/%Y")


def add_week_label(df, date_col="Call Time", out_col="week"):
    """
    Add week labels to call data based on call timestamps.

    This function:
    1. Processes caller information (name, number, type)
    2. Converts time columns to seconds
    3. Filters out admin calls
    4. Assigns week numbers based on Monday-to-Monday boundaries

    Week assignments (Monday-to-Monday):
    - Week 1: Most recent Monday through Sunday (based on max date in dataset)
    - Week 2: Previous Monday through Sunday (7 days before Week 1)
    - Week 3+: Everything older

    Args:
        df (pd.DataFrame): Input DataFrame with call data
        date_col (str): Name of the datetime column
        out_col (str): Name for the output week column

    Returns:
        pd.DataFrame: DataFrame with added columns:
            - week: Week number (1, 2, or 3)
            - Caller Name: Extracted caller name
            - Type: "Retail" or "Trade Customer"
            - Caller Number: Extracted phone number
            - Talking: Time in seconds
            - Ringing: Time in seconds
    """
    df = df.copy()

    # Parse dates
    df['Call Time'] = pd.to_datetime(df['Call Time'])
    df['Old Caller ID'] = df['Caller ID']

    # Extract caller information
    df["Type"] = df["Caller ID"].astype(str).str[0].str.isdigit().map({True: "Retail", False: "Trade Customer"})

    # Remove unnecessary columns
    df.drop(columns=['Sentiment', 'Summary', 'Transcription', 'Old Caller ID'],
            inplace=True, errors="ignore")

    # Convert time columns to seconds
    df["Talking"] = pd.to_numeric(df["Talking"].apply(hms_to_seconds), errors="coerce")
    df["Ringing"] = pd.to_numeric(df["Ringing"].apply(hms_to_seconds), errors="coerce")

    # Filter out admin calls
    df = df[~df["Caller ID"].str.contains("Admin Main DID|Admin Divert", na=False)]

    # Calculate week labels based on Monday-to-Monday boundaries
    dt = pd.to_datetime(df[date_col], errors="coerce")
    max_date = dt.max()

    # Find the Monday at or before max_date (0 = Monday, 6 = Sunday)
    days_since_monday = max_date.weekday()
    most_recent_monday = max_date - pd.Timedelta(days=days_since_monday)

    # If max_date is a Monday, we want the PREVIOUS complete week to be Week 1
    # Otherwise, the week containing max_date (up to the most recent Monday) is Week 1
    if days_since_monday == 0:
        # Max date is Monday - use previous week as Week 1
        week1_start = most_recent_monday - pd.Timedelta(days=7)
        week1_end = most_recent_monday
    else:
        # Max date is Tue-Sun - week containing max_date is Week 1
        week1_start = most_recent_monday
        week1_end = most_recent_monday + pd.Timedelta(days=7)

    # Week 2: Previous Monday through Sunday
    week2_start = week1_start - pd.Timedelta(days=7)
    week2_end = week1_start

    # Assign week labels
    labels = np.full(len(df), 3, dtype=int)  # Default to week 3 (older)
    labels[(dt >= week1_start) & (dt < week1_end)] = 1  # Most recent week (Monday-Sunday)
    labels[(dt >= week2_start) & (dt < week2_end)] = 2  # Previous week (Monday-Sunday)

    df[out_col] = labels
    return df


# =============================================================================
# VISUALIZATION UTILITIES
# =============================================================================

def get_week_colors(weeks):
    """
    Get consistent color mapping for weeks across all visualizations.

    Args:
        weeks: Iterable of week numbers

    Returns:
        dict: Mapping of {week_number: color}
    """
    return {w: WEEK_COLORS.get(w, "gray") for w in weeks}


def normalize_customer_type(type_value):
    """
    Normalize customer type labels to standard format.

    Args:
        type_value: Customer type string (case-insensitive)

    Returns:
        str: Normalized type ("Retail" or "Trade Customer")
    """
    if pd.isna(type_value):
        return type_value

    normalized = str(type_value).strip().lower()
    return CUSTOMER_TYPE_MAPPING.get(normalized, type_value)


def convert_to_seconds(series):
    """
    Convert a time series to seconds handling various formats.

    Handles:
    - Timedelta objects
    - Numeric values (assumed to be seconds)
    - String representations

    Args:
        series (pd.Series): Time series in various formats

    Returns:
        pd.Series: Time values in seconds
    """
    if pd.api.types.is_timedelta64_dtype(series):
        return series.dt.total_seconds()
    elif pd.api.types.is_numeric_dtype(series):
        return series.astype(float)
    else:
        return pd.to_timedelta(series, errors="coerce").dt.total_seconds()


# =============================================================================
# PLOTTING FUNCTIONS
# =============================================================================

def plot_avg_wait_and_talk_grouped(
    df,
    waiting_col="Waiting Time",
    talk_col="Talk Time",
    type_col="Type",
    week_col="week",
    types=("Retail", "Trade Customer"),
    weeks=(2, 1),
    title="Average Waiting & Talk Time by Customer Type",
    save_html_path=None,
    save_png_path=None,
):
    """
    Create dual subplot visualization of average waiting and talk times.

    Generates two side-by-side grouped bar charts:
    - Left: Average waiting time by customer type and week
    - Right: Average talk time by customer type and week

    Args:
        df (pd.DataFrame): Call data with time and type information
        waiting_col (str): Column name for waiting time
        talk_col (str): Column name for talk time
        type_col (str): Column name for customer type
        week_col (str): Column name for week labels
        types (tuple): Customer types to include
        weeks (tuple): Week numbers to compare (e.g., (2, 1) for weeks 2 and 1)
        title (str): Chart title
        save_html_path (str, optional): Path to save interactive HTML
        save_png_path (str, optional): Path to save static PNG

    Returns:
        plotly.graph_objects.Figure: Interactive Plotly figure
    """
    # Prepare data
    data = df.copy()
    data[type_col] = data[type_col].map(normalize_customer_type)

    # Get max date for week label calculation
    max_date = pd.to_datetime(df['Call Time']).max() if 'Call Time' in df.columns else pd.Timestamp.now()

    # Filter for requested types and weeks
    want_types = list(types)
    data = data[data[type_col].isin(want_types)]
    data = data[data[week_col].isin(weeks)]

    # Convert times to seconds
    data["_wait_s"] = convert_to_seconds(data[waiting_col])
    data["_talk_s"] = convert_to_seconds(data[talk_col]) if talk_col in data.columns else np.nan

    # Calculate aggregations
    idx = pd.MultiIndex.from_product([want_types, list(weeks)], names=[type_col, week_col])
    grp = data.groupby([type_col, week_col], dropna=False)

    mean_wait_min = (grp["_wait_s"].mean() / 60.0).reindex(idx)
    mean_talk_min = (grp["_talk_s"].mean() / 60.0).reindex(idx)
    count_wait = grp["_wait_s"].apply(lambda s: s.notna().sum()).reindex(idx).fillna(0).astype(int)
    count_talk = grp["_talk_s"].apply(lambda s: s.notna().sum()).reindex(idx).fillna(0).astype(int)

    # Create subplots
    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=("Average Waiting Time (min)", "Average Talk Time (min)"),
        horizontal_spacing=0.12
    )

    week_colors = get_week_colors(weeks)

    # Helper function to add grouped bars
    def add_grouped_bars(means, counts, col_idx, metric_name):
        for w in weeks:
            y_vals = [means.loc[(t, w)] for t in want_types]
            call_counts = [counts.loc[(t, w)] for t in want_types]
            week_label = f"Week {get_week_date_label(w, max_date)}"

            fig.add_bar(
                x=want_types,
                y=y_vals,
                name=week_label,
                marker_color=week_colors[w],
                customdata=np.array(call_counts).reshape(-1, 1),
                hovertemplate=(
                    "Type: %{x}<br>"
                    f"{week_label}<br>"
                    f"Avg {metric_name}: %{{y:.2f}} min<br>"
                    "Total Calls: %{customdata[0]}<extra></extra>"
                ),
                row=1, col=col_idx,
                showlegend=(col_idx == 1)  # Only show legend once
            )

        fig.update_xaxes(title_text="Customer Type", row=1, col=col_idx)
        fig.update_yaxes(title_text=f"Average {metric_name} (minutes)", row=1, col=col_idx)

    # Add bars for both metrics
    add_grouped_bars(mean_wait_min, count_wait, 1, "Waiting Time")
    add_grouped_bars(mean_talk_min, count_talk, 2, "Talk Time")

    # Update layout
    fig.update_layout(
        title=title,
        title_x=0.5,
        barmode="group",
        bargap=0.25,
        bargroupgap=0.1,
        legend_title="Week",
        hovermode="x unified"
    )

    # Save outputs
    if save_html_path:
        fig.write_html(save_html_path, include_plotlyjs="cdn")
    if save_png_path:
        try:
            fig.write_image(save_png_path, format="png", scale=2)
        except Exception as e:
            print(f"Warning: PNG export failed - {e}. Install 'kaleido' to enable.")

    return fig


def plot_grouped_avg_time_by_type(
    df,
    time_col,
    type_col="Type",
    types=("Retail", "Trade Customer"),
    week_col="week",
    weeks=(2, 1),
    title_prefix="Average Time",
    save_html_path=None,
    save_png_path=None,
):
    """
    Create grouped bar chart for a single time metric across weeks and types.

    Args:
        df (pd.DataFrame): Call data
        time_col (str): Column name for the time metric to plot
        type_col (str): Column name for customer type
        types (tuple): Customer types to include
        week_col (str): Column name for week labels
        weeks (tuple): Week numbers to compare
        title_prefix (str): Prefix for chart title
        save_html_path (str, optional): Path to save interactive HTML
        save_png_path (str, optional): Path to save static PNG

    Returns:
        plotly.graph_objects.Figure: Interactive Plotly figure

    Raises:
        ValueError: If specified columns don't exist in DataFrame
    """
    # Validate inputs
    if time_col not in df.columns:
        raise ValueError(f"Column '{time_col}' not found in DataFrame.")
    if week_col not in df.columns:
        raise ValueError(f"Column '{week_col}' not found. Run add_week_label() first.")

    # Prepare data
    data = df.copy()
    data = data[data[type_col].isin(types)]
    data = data[data[week_col].isin(weeks)]

    # Get max date for week label calculation
    max_date = pd.to_datetime(df['Call Time']).max() if 'Call Time' in df.columns else pd.Timestamp.now()

    # Convert to seconds
    data["_secs"] = convert_to_seconds(data[time_col])

    # Calculate aggregations
    grp = data.groupby([type_col, week_col], dropna=False)
    means_min = (grp["_secs"].mean() / 60.0)
    counts = grp.size()

    idx = pd.MultiIndex.from_product([list(types), list(weeks)], names=[type_col, week_col])
    means_min = means_min.reindex(idx)
    counts = counts.reindex(idx).fillna(0).astype(int)

    # Create plot
    week_colors = get_week_colors(weeks)
    fig = go.Figure()

    for w in weeks:
        y_vals = [means_min.loc[(t, w)] for t in types]
        call_counts = [counts.loc[(t, w)] for t in types]
        week_label = f"Week {get_week_date_label(w, max_date)}"

        fig.add_bar(
            x=list(types),
            y=y_vals,
            name=week_label,
            marker_color=week_colors[w],
            customdata=np.array(call_counts).reshape(-1, 1),
            hovertemplate=(
                "Type: %{x}<br>"
                f"{week_label}<br>"
                f"Avg: %{{y:.2f}} min<br>"
                "Total Calls: %{customdata[0]}<extra></extra>"
            ),
        )

    fig.update_layout(
        title=f"{title_prefix} by Customer Type",
        xaxis_title="Customer Type",
        yaxis_title="Average Time (minutes)",
        title_x=0.5,
        barmode="group",
        bargap=0.25,
        bargroupgap=0.1,
        legend_title="Week",
        hovermode="x",
    )

    # Save outputs
    if save_html_path:
        fig.write_html(save_html_path, include_plotlyjs="cdn")
    if save_png_path:
        try:
            fig.write_image(save_png_path, format="png", scale=2)
        except Exception as e:
            print(f"Warning: PNG export failed - {e}. Install 'kaleido' to enable.")

    return fig


def plot_abandoned_by_day_of_week(
    df,
    waiting_col="Waiting Time",
    date_col="Call Time",
    week_col="week",
    weeks=(2, 1),
    title="Abandoned Calls by Day of Week",
    all_calls_df=None,
):
    """
    Visualize abandoned calls distribution across days of the week.

    Args:
        df (pd.DataFrame): Abandoned calls data
        waiting_col (str): Column name for waiting time
        date_col (str): Column name for call timestamp
        week_col (str): Column name for week labels
        weeks (tuple): Week numbers to compare
        title (str): Chart title
        all_calls_df (pd.DataFrame, optional): All calls data for total/answered stats

    Returns:
        plotly.graph_objects.Figure: Interactive Plotly figure
    """
    data = df.copy()

    # Get max date for week label calculation
    max_date = pd.to_datetime(df[date_col]).max() if date_col in df.columns else pd.Timestamp.now()

    # Filter for requested weeks
    data = data[data[week_col].isin(weeks)]

    # Extract day of week
    data['day_of_week'] = pd.to_datetime(data[date_col]).dt.day_name()

    # Convert waiting time to minutes for statistics
    if waiting_col in data.columns:
        data['wait_minutes'] = data[waiting_col] / 60.0
    else:
        data['wait_minutes'] = 0

    # Define day order (Monday through Sunday)
    day_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

    # Calculate statistics for each day and week
    stats_by_day_week = {}
    for w in weeks:
        for day in day_order:
            day_week_data = data[(data[week_col] == w) & (data['day_of_week'] == day)]

            if len(day_week_data) > 0:
                # Calculate waiting time statistics
                wait_times = day_week_data['wait_minutes'].dropna()
                min_wait = wait_times.min() if len(wait_times) > 0 else 0
                max_wait = wait_times.max() if len(wait_times) > 0 else 0
                mode_wait = wait_times.mode()[0] if len(wait_times) > 0 and len(wait_times.mode()) > 0 else wait_times.mean()

                stats_by_day_week[(w, day)] = {
                    'abandoned_count': len(day_week_data),
                    'min_wait': min_wait,
                    'mode_wait': mode_wait,
                    'max_wait': max_wait,
                }
            else:
                stats_by_day_week[(w, day)] = {
                    'abandoned_count': 0,
                    'min_wait': 0,
                    'mode_wait': 0,
                    'max_wait': 0,
                }

    # Calculate total calls and answered calls if all_calls_df is provided
    if all_calls_df is not None:
        all_data = all_calls_df.copy()
        all_data = all_data[all_data[week_col].isin(weeks)]
        all_data['day_of_week'] = pd.to_datetime(all_data[date_col]).dt.day_name()

        # Filter to only include INBOUND calls (exclude Outbound)
        # AND only count unique calls (deduplicate by Call ID and Call Time)
        if 'Direction' in all_data.columns:
            all_data = all_data[all_data['Direction'].isin(['Inbound', 'Inbound Queue'])]

        # Deduplicate: Group by Call ID and Call Time to count unique inbound calls
        # Keep only one row per unique call (prefer 'Answered' status over others)
        if 'Call ID' in all_data.columns:
            # Sort by Status so 'Answered' comes first
            all_data['status_priority'] = all_data['Status'].apply(
                lambda x: 0 if x == 'Answered' else 1 if x == 'Unanswered' else 2
            )
            all_data = all_data.sort_values('status_priority')
            all_data = all_data.drop_duplicates(subset=['Call ID', date_col], keep='first')
            all_data = all_data.drop(columns=['status_priority'])

        for w in weeks:
            for day in day_order:
                day_week_calls = all_data[(all_data[week_col] == w) & (all_data['day_of_week'] == day)]

                # Count total unique inbound calls and answered calls (Status == 'Answered')
                total_calls = len(day_week_calls)
                answered_calls = len(day_week_calls[day_week_calls['Status'] == 'Answered']) if 'Status' in day_week_calls.columns else 0

                if (w, day) in stats_by_day_week:
                    stats_by_day_week[(w, day)]['total_calls'] = total_calls
                    stats_by_day_week[(w, day)]['answered_calls'] = answered_calls

    # Count abandoned calls by day and week
    grp = data.groupby([week_col, 'day_of_week']).size().reset_index(name='count')
    pivot = grp.pivot(index='day_of_week', columns=week_col, values='count').reindex(day_order).fillna(0)

    # Create plot
    week_colors = get_week_colors(weeks)
    fig = go.Figure()

    for w in weeks:
        if w in pivot.columns:
            week_label = f"Week {get_week_date_label(w, max_date)}"
            y_values = []
            custom_data = []

            for day in day_order:
                count = pivot.loc[day, w] if day in pivot.index else 0
                y_values.append(count)

                stats = stats_by_day_week.get((w, day), {})
                custom_data.append([
                    stats.get('total_calls', 0),
                    stats.get('answered_calls', 0),
                    stats.get('abandoned_count', 0),
                    stats.get('min_wait', 0),
                    stats.get('mode_wait', 0),
                    stats.get('max_wait', 0),
                ])

            # Format custom data with proper time formatting (minutes or seconds)
            formatted_custom_data = []
            for stats_list in custom_data:
                total_calls = stats_list[0]
                answered_calls = stats_list[1]
                abandoned_count = stats_list[2]
                min_wait = stats_list[3]
                mode_wait = stats_list[4]
                max_wait = stats_list[5]

                # Format times: if < 1 minute, show in seconds; otherwise show in minutes
                min_wait_str = f"{int(min_wait * 60)}s" if min_wait < 1 else f"{min_wait:.1f}m"
                mode_wait_str = f"{int(mode_wait * 60)}s" if mode_wait < 1 else f"{mode_wait:.1f}m"
                max_wait_str = f"{int(max_wait * 60)}s" if max_wait < 1 else f"{max_wait:.1f}m"

                formatted_custom_data.append([
                    total_calls,
                    answered_calls,
                    abandoned_count,
                    min_wait_str,
                    mode_wait_str,
                    max_wait_str,
                ])

            fig.add_bar(
                x=day_order,
                y=y_values,
                name=week_label,
                marker_color=week_colors[w],
                customdata=formatted_custom_data,
                hovertemplate=(
                    "Day: %{x}<br>"
                    f"{week_label}<br>"
                    "Abandoned Calls: %{y}<br>"
                    + ("Total Calls: %{customdata[0]}<br>" if all_calls_df is not None else "") +
                    ("Answered Calls: %{customdata[1]}<br>" if all_calls_df is not None else "") +
                    "Min Wait: %{customdata[3]}<br>"
                    "Average Wait: %{customdata[4]}<br>"
                    "Max Wait: %{customdata[5]}<extra></extra>"
                ),
            )

    fig.update_layout(
        title=title,
        title_x=0.5,
        xaxis_title="Day of Week",
        yaxis_title="Number of Abandoned Calls",
        barmode="group",
        bargap=0.25,
        bargroupgap=0.1,
        legend_title="Week",
        hovermode="x",
    )

    return fig


def plot_avg_waiting_time(
    df,
    waiting_col="Waiting Time",
    type_col="Type",
    week_col="week",
    types=("Retail", "Trade Customer"),
    weeks=(2, 1),
    title="Average Waiting Time (minutes)",
):
    """
    Create bar chart showing average waiting time for all customer types.

    Always displays both customer types even if one has zero data.

    Args:
        df (pd.DataFrame): Call data with waiting times
        waiting_col (str): Column name for waiting time
        type_col (str): Column name for customer type
        week_col (str): Column name for week labels
        types (tuple): Customer types to include
        weeks (tuple): Week numbers to compare
        title (str): Chart title

    Returns:
        plotly.graph_objects.Figure: Interactive Plotly figure
    """
    # Prepare data
    data = df.copy()
    data[type_col] = data[type_col].map(normalize_customer_type)

    # Get max date for week label calculation
    max_date = pd.to_datetime(df['Call Time']).max() if 'Call Time' in df.columns else pd.Timestamp.now()

    # Filter data
    want_types = list(types)
    data = data[data[type_col].isin(want_types)]
    data = data[data[week_col].isin(weeks)]

    # Convert to seconds
    data["_wait_s"] = convert_to_seconds(data[waiting_col])

    # Calculate aggregations for all type-week combinations
    idx = pd.MultiIndex.from_product([want_types, list(weeks)], names=[type_col, week_col])
    grp = data.groupby([type_col, week_col], dropna=False)

    mean_wait_min = (grp["_wait_s"].mean() / 60.0).reindex(idx).fillna(0)
    count_wait = grp["_wait_s"].apply(lambda s: s.notna().sum()).reindex(idx).fillna(0).astype(int)

    # Create plot
    week_colors = get_week_colors(weeks)
    fig = go.Figure()

    for w in weeks:
        y_vals = [mean_wait_min.loc[(t, w)] for t in want_types]
        call_counts = [count_wait.loc[(t, w)] for t in want_types]
        week_label = f"Week {get_week_date_label(w, max_date)}"

        fig.add_bar(
            x=want_types,
            y=y_vals,
            name=week_label,
            marker_color=week_colors[w],
            customdata=np.array(call_counts).reshape(-1, 1),
            hovertemplate=(
                "Type: %{x}<br>"
                f"{week_label}<br>"
                "Avg Waiting Time: %{y:.2f} min<br>"
                "Total Calls: %{customdata[0]}<extra></extra>"
            ),
        )

    fig.update_layout(
        title=title,
        title_x=0.5,
        xaxis_title="Customer Type",
        yaxis_title="Average Time (minutes)",
        barmode="group",
        bargap=0.25,
        bargroupgap=0.1,
        legend_title="Week",
        hovermode="x",
    )

    return fig


def plot_avg_talking_grouped(df, **kwargs):
    """
    Convenience function to plot average talking time.

    Wrapper around plot_grouped_avg_time_by_type() specifically for talking time.

    Args:
        df (pd.DataFrame): Call data
        **kwargs: Additional arguments passed to plot_grouped_avg_time_by_type()

    Returns:
        plotly.graph_objects.Figure: Interactive Plotly figure
    """
    return plot_grouped_avg_time_by_type(
        df,
        time_col="Talking",
        title_prefix="Average Talking Time",
        **kwargs
    )
