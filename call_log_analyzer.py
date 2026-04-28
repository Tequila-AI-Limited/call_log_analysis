import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import psycopg2
from psycopg2.extras import execute_values
import os
from cleaning import run_cleaning
import glob

# Database Configuration
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'kmc.tequila-ai.com'),
    'port': os.getenv('DB_PORT', '5432'),
    'database': os.getenv('DB_NAME', 'tequila_ai_reporting'),
    'user': os.getenv('DB_USER', 'james'),
    'password': os.getenv('DB_PASSWORD'),
    'sslmode': os.getenv('DB_SSLMODE', 'require')
}

def save_to_database(df, table_name='call_logs'):
    """Save DataFrame to PostgreSQL database using upsert on Call ID to preserve history."""
    try:
        print(f"Connecting to database to save {len(df)} rows...")
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()

        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                "Call ID" TEXT PRIMARY KEY,
                call_start TIMESTAMP,
                from_number TEXT,
                to_number TEXT,
                directions TEXT,
                statuses TEXT,
                ringing_total_sec INTEGER,
                talking_total_sec INTEGER,
                customer_type TEXT,
                call_activity_details TEXT,
                is_answered BOOLEAN,
                is_abandoned BOOLEAN,
                date DATE,
                day_name TEXT,
                week INTEGER,
                week_start DATE
            );
        """)

        db_cols = [
            'Call ID', 'call_start', 'from_number', 'to_number',
            'directions', 'statuses', 'ringing_total_sec', 'talking_total_sec',
            'customer_type', 'call_activity_details', 'is_answered', 'is_abandoned',
            'date', 'day_name', 'week', 'week_start'
        ]
        quoted_cols = [f'"{c}"' for c in db_cols]
        values = [tuple(x) for x in df[db_cols].to_numpy()]

        update_set = ', '.join(
            f'{q} = EXCLUDED.{q}' for q in quoted_cols if q != '"Call ID"'
        )
        insert_sql = f"""
            INSERT INTO {table_name} ({', '.join(quoted_cols)})
            VALUES %s
            ON CONFLICT ("Call ID") DO UPDATE SET {update_set}
        """
        execute_values(cursor, insert_sql, values)

        conn.commit()
        print(f"Successfully saved {len(df)} rows to table '{table_name}'.")

    except Exception as e:
        print(f"Error saving to database: {e}")
        if 'conn' in locals():
            conn.rollback()
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def calculate_mode(series):
    """Calculate mode of a series, handling empty cases."""
    mode = series.mode()
    if not mode.empty:
        return mode.iloc[0]
    return 0

def get_week_date_label(week_num, max_date):
    """Generate week date label based on week number and max date.
    
    Week 1 = max_date going back 7 days
    Week 2 = 7 days before Week 1
    Week 3+ = older
    """
    if not isinstance(max_date, pd.Timestamp):
        max_date = pd.to_datetime(max_date)
    
    # Calculate based on max date going backwards
    # Week 1 = last 7 days from max_date
    # Week 2 = 7 days before that, etc.
    days_back = (week_num - 1) * 7
    week_end = max_date - pd.Timedelta(days=days_back)
    week_start = week_end - pd.Timedelta(days=6)  # 7 days total (inclusive)
    
    return week_start.strftime('%d/%m/%Y')

def generate_plots(df, abandoned_df):
    """Generate combined Plotly HTML with three subplots."""
    from plotly.subplots import make_subplots
    
    # Helper for time formatting
    def format_time_hover(seconds):
        if pd.isna(seconds):
            return "0s"
        if seconds < 60:
            return f"{int(seconds)}s"
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m {s}s"

    # --- Prepare Data for Charts 1 & 2 (Wait/Talk Time) ---
    # Filter for Week 1 and 2, and exclude 'unknown' customer type
    df_recent = df[df['week'].isin([1, 2])].copy()
    df_recent = df_recent[df_recent['customer_type'].isin(['retail', 'trade'])]
    
    # Normalize customer type labels for display
    df_recent['customer_type_display'] = df_recent['customer_type'].map({
        'retail': 'Retail',
        'trade': 'Trade Customer'
    })
    
    # Group by Week and Customer Type
    # Using MEAN as requested ("Average Talking Time")
    grouped = df_recent.groupby(['week', 'customer_type_display']).agg(
        avg_wait_time=('ringing_total_sec', 'mean'),
        avg_talk_time=('talking_total_sec', 'mean'),
        call_count=('ringing_total_sec', 'count')
    ).reset_index()
    
    # Define colors: This Week (blue), Last Week (orange)
    color_map = {1: '#2391DC', 2: '#DC6E23'}
    
    # Get max date for week labels
    max_date = df['call_start'].max() if 'call_start' in df.columns else pd.Timestamp.now()
    
    # Helper function to get user-friendly week labels
    # Week 1 = "This Week" (Most Recent)
    # Week 2 = "Last Week" (Previous)
    def get_week_label_display(week):
        if week == 1:
            return "This Week"
        elif week == 2:
            return "Last Week"
        else:
            return f"Week {week}"
    
    # Chart 1: Average Wait Time
    fig_waiting = go.Figure()
    customer_types = ['Retail', 'Trade Customer']
    
    # NEW: Capture metrics from plot data for "Bottom Up" consistency
    plot_derived_metrics = {}
    
    # Loop order: Week 1 (This Week) then Week 2 (Last Week) for consistent legend ordering if needed
    # Or keep [2, 1] if we want "Last Week" on left/top? Usually "This Week" is first or second.
    # Let's use [1, 2] to process This Week first.
    for week in [1, 2]:
        week_data = grouped[grouped['week'] == week]
        y_vals = []
        call_counts = []
        hover_texts = []
        
        # Determine metric prefix
        # Week 1 = "This Week" -> metrics['week1_...']
        # Week 2 = "Last Week" -> metrics['week2_...']
        metric_week = f"week{week}"
        
        for ctype in customer_types:
            row = week_data[week_data['customer_type_display'] == ctype]
            if len(row) > 0:
                val = row['avg_wait_time'].values[0]
                count = int(row['call_count'].values[0])
                y_vals.append(val) # Plot in seconds
                call_counts.append(count)
                hover_texts.append(format_time_hover(val))
                
                # Capture count for metrics
                metric_key = f"{metric_week}_{ctype.lower().split()[0]}_calls" # week1_retail_calls
                plot_derived_metrics[metric_key] = count
                
                # Also capture 'total' key for consistency
                total_key = f"{metric_week}_{ctype.lower().split()[0]}_total"
                plot_derived_metrics[total_key] = count
            else:
                y_vals.append(0)
                call_counts.append(0)
                hover_texts.append("0s")
                
                # Capture 0 count
                metric_key = f"{metric_week}_{ctype.lower().split()[0]}_calls"
                plot_derived_metrics[metric_key] = 0
                total_key = f"{metric_week}_{ctype.lower().split()[0]}_total"
                plot_derived_metrics[total_key] = 0
        
        week_label = get_week_label_display(week)
        
        fig_waiting.add_trace(go.Bar(
            name=week_label,
            x=customer_types,
            y=y_vals,
            marker_color=color_map[week],
            text=[week_label] * len(customer_types),  # Add label inside bar
            textposition='auto',
            customdata=np.array(list(zip(call_counts, hover_texts))),
            hovertemplate=(
                "Type: %{x}<br>"
                f"{week_label}<br>"
                "Avg Waiting Time: %{customdata[1]}<br>"
                "Total Calls: %{customdata[0]}<extra></extra>"
            )
        ))

    # Chart 2: Average Talk Time
    fig_talk = go.Figure()
    
    for week in [1, 2]:
        week_data = grouped[grouped['week'] == week]
        y_vals = []
        call_counts = []
        hover_texts = []
        
        for ctype in customer_types:
            row = week_data[week_data['customer_type_display'] == ctype]
            if len(row) > 0:
                val = row['avg_talk_time'].values[0]
                count = int(row['call_count'].values[0])
                y_vals.append(val / 60) # Plot in minutes
                call_counts.append(count)
                hover_texts.append(format_time_hover(val))
            else:
                y_vals.append(0)
                call_counts.append(0)
                hover_texts.append("0s")
        
        week_label = get_week_label_display(week)
        
        fig_talk.add_trace(go.Bar(
            name=week_label,
            x=customer_types,
            y=y_vals,
            marker_color=color_map[week],
            text=[week_label] * len(customer_types),  # Add label inside bar
            textposition='auto',
            customdata=np.array(list(zip(call_counts, hover_texts))),
            hovertemplate=(
                "Type: %{x}<br>"
                f"{week_label}<br>"
                "Avg Talk Time: %{customdata[1]}<br>"
                "Total Calls: %{customdata[0]}<extra></extra>"
            )
        ))

    # --- Prepare Data for Chart 3 (Abandoned by Day) ---
    fig_abandoned = go.Figure()
    day_counts = pd.DataFrame()
    
    if not abandoned_df.empty:
        # Process abandoned data
        abd = abandoned_df.copy()
        # Robust datetime parsing
        abd['Call Time'] = pd.to_datetime(abd['Call Time'], errors='coerce')
        
        # Week is already calculated in analyze_calls, so we just use it!
        # Ensure week column exists
        if 'week' not in abd.columns:
             print("Warning: 'week' column missing in abandoned_df passed to generate_plots")
             return ""

        # Filter weeks 1 & 2
        abd = abd[abd['week'].isin([1, 2])].copy()
        abd['day_of_week'] = abd['Call Time'].dt.day_name()
        
        # Parse Waiting Time to seconds for stats
        def parse_wait(x):
            try:
                if pd.isna(x): return 0
                parts = str(x).split(':')
                if len(parts) == 3: return int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
                return 0
            except: return 0
        abd['wait_sec'] = abd['Waiting Time'].apply(parse_wait)
        
        # Prepare main df for total/answered stats
        main_df = df[df['week'].isin([1, 2])].copy()
        main_df['day_of_week'] = main_df['call_start'].dt.day_name()
        
        days_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

        week1_abd_data = pd.DataFrame()
        week2_abd_data = pd.DataFrame()

        for week in [1, 2]:
            # Main Data
            week_main = main_df[main_df['week'] == week]
            
            # Abandoned Data - Use same week!
            week_abd = abd[abd['week'] == week]
            
            # Capture abandoned metrics for "Bottom Up" consistency
            # Determine metric prefix
            metric_week = f"week{week}"
            
            # Total abandoned for this week
            total_abd_count = len(week_abd)
            plot_derived_metrics[f"{metric_week}_abandoned_total"] = total_abd_count
            
            # Calculate retail/trade split for metrics update
            retail_abd = len(week_abd[week_abd['customer_type'] == 'retail'])
            trade_abd = len(week_abd[week_abd['customer_type'] == 'trade'])
            
            plot_derived_metrics[f"{metric_week}_retail_abandoned"] = retail_abd
            plot_derived_metrics[f"{metric_week}_trade_abandoned"] = trade_abd
            
            # Update Total Calls Metric = Retail + Trade + Abandoned
            current_retail = plot_derived_metrics.get(f"{metric_week}_retail_total", 0)
            current_trade = plot_derived_metrics.get(f"{metric_week}_trade_total", 0)
            plot_derived_metrics[f"{metric_week}_calls"] = current_retail + current_trade + total_abd_count
            
            y_vals = []
            custom_data = []
            
            for day in days_order:
                # Abandoned stats
                day_abd = week_abd[week_abd['day_of_week'] == day]
                abd_count = len(day_abd)
                y_vals.append(abd_count)
                
                # Wait time stats
                if not day_abd.empty:
                    min_w = day_abd['wait_sec'].min()
                    avg_w = day_abd['wait_sec'].mean()
                    max_w = day_abd['wait_sec'].max()
                else:
                    min_w = avg_w = max_w = 0
                
                # Main call stats
                day_main = week_main[week_main['day_of_week'] == day]
                
                # Count ALL main log calls (not just answered) to match executive summary
                main_calls_count = len(day_main)
                answered_calls = day_main['is_answered'].sum()
                
                # Total = All Main Log Calls + Abandoned
                # This matches the executive summary formula: Retail + Trade + Abandoned = Total
                total_calls = main_calls_count + abd_count
                
                # Voicemail/Other = Total Main - Answered (calls in main log but not answered)
                voicemail_other = main_calls_count - int(answered_calls)
                
                custom_data.append([
                    total_calls,                # 0 - Main calls + Abandoned
                    int(answered_calls),        # 1 - Answered calls only
                    abd_count,                  # 2 - Abandoned
                    format_time_hover(min_w),   # 3
                    format_time_hover(avg_w),   # 4
                    format_time_hover(max_w),   # 5
                    voicemail_other             # 6 - Voicemail/Other
                ])
            
            week_label = get_week_label_display(week)

            fig_abandoned.add_trace(go.Bar(
                name=week_label,
                x=days_order,
                y=y_vals,
                marker_color=color_map[week],
                customdata=custom_data,
                hovertemplate=(
                    "Day: %{x}<br>"
                    f"{week_label}<br>"
                    "Abandoned Calls: %{y}<br>"
                    "Answered Calls: %{customdata[1]}<br>"
                    "Voicemail/Other: %{customdata[6]}<br>"
                    "Total Calls: %{customdata[0]}<br>"
                    "Min Wait: %{customdata[3]}<br>"
                    "Average Wait: %{customdata[4]}<br>"
                    "Max Wait: %{customdata[5]}<extra></extra>"
                )
            ))
            
            # Store for annotation calculation
            if week == 1: week1_abd_data = week_abd
            if week == 2: week2_abd_data = week_abd
            if week == 1 or week == 2: day_counts = pd.concat([day_counts, week_abd])

    # --- Combine Plots ---
    combined_fig = make_subplots(
        rows=3, cols=1,
        subplot_titles=(
            "Average Waiting Time",
            "Average Talking Time",
            "Abandoned Calls by Day of Week"
        ),
        specs=[[{}], [{}], [{}]],
        vertical_spacing=0.08,
        row_heights=[0.33, 0.33, 0.33]
    )
    
    for trace in fig_waiting.data:
        trace.showlegend = False
        combined_fig.add_trace(trace, row=1, col=1)
        
    for trace in fig_talk.data:
        trace.showlegend = False
        combined_fig.add_trace(trace, row=2, col=1)
        
    for trace in fig_abandoned.data:
        trace.showlegend = True # Show legend on bottom plot
        combined_fig.add_trace(trace, row=3, col=1)
        
    # --- Annotation ---
    if not abandoned_df.empty:
        # Recalculate global stats for annotation
        w1_mean = week1_abd_data['wait_sec'].mean() if not week1_abd_data.empty else 0
        w2_mean = week2_abd_data['wait_sec'].mean() if not week2_abd_data.empty else 0
        w1_count = len(week1_abd_data)
        w2_count = len(week2_abd_data)
        
        # Dynamic Y
        max_y = 10
        if not day_counts.empty:
             # Group by week/day to find max bar height
             cts = day_counts.groupby(['week', 'day_of_week']).size()
             if not cts.empty: max_y = cts.max()
        
        annotation_text = (
            f"<b>Avg Time to Abandon:</b><br>"
            f"Week {get_week_date_label(1, max_date)}: {format_time_hover(w1_mean)} ({w1_count} calls)<br>"
            f"Week {get_week_date_label(2, max_date)}: {format_time_hover(w2_mean)} ({w2_count} calls)"
        )
        
        combined_fig.add_annotation(
            text=annotation_text,
            xref="x3", yref="y3",
            x=-0.4, y=max_y * 0.8,
            showarrow=False,
            align="left",
            xanchor="left",
            yanchor="middle",
            bgcolor="rgba(255, 255, 255, 0.8)",
            bordercolor="black",
            borderwidth=1,
            font=dict(size=10)
        )

    # Update axis labels
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
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.05,
            xanchor="center",
            x=0.5
        ),
        margin=dict(l=20, r=20, t=60, b=50)
    )
    
    return {
        'combined_plot': combined_fig.to_html(
            full_html=False, 
            include_plotlyjs='cdn',
            config={'responsive': True, 'displayModeBar': False}
        )
    }, plot_derived_metrics

def load_abandoned_calls(data_dir='data'):
    """Load all abandoned calls CSV files."""
    files = glob.glob(os.path.join(data_dir, 'AbandonedCalls*.csv'))
    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f)
            dfs.append(df)
        except Exception as e:
            print(f"Error reading {f}: {e}")
    
    if dfs:
        combined_df = pd.concat(dfs, ignore_index=True)
        
        # Clean Caller ID immediately to remove .0 suffix globally
        if 'Caller ID' in combined_df.columns:
            combined_df['Caller ID'] = combined_df['Caller ID'].astype(str).apply(
                lambda x: x[:-2] if x.endswith('.0') else x
            )
            
        # Deduplicate based on Caller ID and Call Time
        before_dedup = len(combined_df)
        combined_df = combined_df.drop_duplicates(subset=['Caller ID', 'Call Time'])
        print(f"Deduplicated abandoned logs: {before_dedup} -> {len(combined_df)}")
            
        # Save combined file
        output_path = os.path.join(data_dir, 'combined_abandoned_call_logs.csv')
        combined_df.to_csv(output_path, index=False)
        print(f"Saved combined abandoned calls to {output_path}")
        return combined_df
    return pd.DataFrame()

def analyze_abandoned_calls(abandoned_df, main_df):
    """Analyze abandoned calls with customer type mapping."""
    if abandoned_df.empty:
        return {}

    # Create mapping from Caller ID to Customer Type using main_df
    # Logic: Identify known Trade numbers. Everything else is Retail.
    
    # Get set of known trade numbers
    # Ensure main df numbers are strings and clean
    trade_numbers = set(main_df[main_df['customer_type'] == 'trade']['from_number'].astype(str).apply(lambda x: x.replace('.0', '')).unique())
    
    # Apply mapping: Check if Caller ID is in trade_numbers
    # Caller ID is already cleaned in load_abandoned_calls
    abandoned_df['customer_type'] = abandoned_df['Caller ID'].apply(
        lambda x: 'trade' if str(x) in trade_numbers else 'retail'
    )
    
    # Calculate metrics
    metrics = {
        'total_abandoned_raw': len(abandoned_df),
        'retail_abandoned': len(abandoned_df[abandoned_df['customer_type'] == 'retail']),
        'trade_abandoned': len(abandoned_df[abandoned_df['customer_type'] == 'trade']),
        'unknown_abandoned': 0 # No longer used
    }
    
    return metrics

import re

def clean_phone_for_match(phone):
    """Normalize a phone number string for matching (strips country prefix and leading zero)."""
    s = str(phone).strip()
    s = s.replace(' ', '').replace('-', '').replace('.', '').replace('(', '').replace(')', '')
    if s.startswith('+353'):
        s = s[4:]
    elif s.startswith('00353'):
        s = s[5:]
    elif s.startswith('353'):
        s = s[3:]
    if s.startswith('0'):
        s = s[1:]
    return s

def analyze_journey(main_df, abandoned_df):
    """Analyze caller journey including Queue, Voicemail, OOO, and Termination."""
    journey_stats = {}
    
    # --- 1. Main Log Analysis (Answered/Processed Calls) ---
    # Group by Call ID to get full journey details
    # Fill NA in Call Activity Details
    if 'call_activity_details' not in main_df.columns:
        # Fallback if cleaning didn't add it (should not happen if cleaning.py is updated)
        main_df['call_activity_details'] = ''
        
    main_df['call_activity_details'] = main_df['call_activity_details'].fillna('')
    
    # Main DF is already aggregated by Call ID from cleaning.py, so we don't need to group again
    # But we need to ensure we are working with the right structure.
    journey_df = main_df.copy()
    
    # Queue Usage
    journey_stats['queue_calls'] = len(journey_df[journey_df['call_activity_details'].str.contains('Queue', case=False)])
    
    # Out of Hours (Explicit OOO)
    journey_stats['ooo_calls'] = len(journey_df[journey_df['call_activity_details'].str.contains('Out of office', case=False)])
    
    # Voicemail (Voice Agent)
    journey_stats['voicemail_calls'] = len(journey_df[journey_df['call_activity_details'].str.contains('Voice Agent', case=False)])
    
    # Termination Analysis
    def extract_terminator(details):
        if 'Ended by Voice Agent' in details: return 'System'
        if 'Ended by' in details:
            # Check if ended by a known agent name (usually contains comma or 'Sales')
            # Or if ended by a number (likely customer)
            # Heuristic: If "Ended by [Number]", it's customer. If "Ended by [Name]", it's agent.
            # But names can be complex. Let's look for digits in the terminator.
            m = re.search(r'Ended by ([^:|]+)', details)
            if m:
                terminator = m.group(1).strip()
                # If terminator contains digits and looks like a phone number, it's customer
                if any(char.isdigit() for char in terminator) and len(terminator) > 5:
                    return 'Customer'
                return 'Agent'
        return 'Unknown'
        
    journey_df['Terminator'] = journey_df['call_activity_details'].apply(extract_terminator)
    term_counts = journey_df['Terminator'].value_counts()
    journey_stats['ended_by_agent'] = term_counts.get('Agent', 0)
    journey_stats['ended_by_customer'] = term_counts.get('Customer', 0)
    journey_stats['ended_by_system'] = term_counts.get('System', 0)
    
    # --- 2. Abandoned Log Analysis (Abandoned Journey) ---
    if not abandoned_df.empty:
        # Agent State Analysis
        if 'Agent State' in abandoned_df.columns:
            logged_out_df = abandoned_df[abandoned_df['Agent State'] == 'Logged Out'].copy()
            journey_stats['abd_agent_logged_out'] = len(logged_out_df)
            journey_stats['abd_agent_logged_in'] = len(abandoned_df[abandoned_df['Agent State'] == 'Logged In'])
            
            # Time breakdown for agents logged out calls
            if not logged_out_df.empty and 'Call Time' in logged_out_df.columns:
                logged_out_df['Call Time'] = pd.to_datetime(logged_out_df['Call Time'], errors='coerce')
                logged_out_df['hour'] = logged_out_df['Call Time'].dt.hour
                logged_out_df['day_of_week'] = logged_out_df['Call Time'].dt.dayofweek
                
                # Count calls before and after operating hours
                before_hours = 0
                after_hours = 0
                during_hours = 0
                
                for idx, row in logged_out_df.iterrows():
                    day = row['day_of_week']
                    hour = row['hour']
                    
                    if pd.isna(day) or pd.isna(hour):
                        continue
                        
                    # Operating hours check
                    if day <= 4:  # Monday-Friday
                        if hour < 8:
                            before_hours += 1
                        elif hour >= 20:
                            after_hours += 1
                        else:
                            during_hours += 1
                    elif day == 5:  # Saturday
                        if hour < 8:
                            before_hours += 1
                        elif hour >= 18:
                            after_hours += 1
                        else:
                            during_hours += 1
                    elif day == 6:  # Sunday
                        if hour < 10:
                            before_hours += 1
                        elif hour >= 16:
                            after_hours += 1
                        else:
                            during_hours += 1
                
                journey_stats['abd_logged_out_before_hours'] = before_hours
                journey_stats['abd_logged_out_after_hours'] = after_hours
                journey_stats['abd_logged_out_during_hours'] = during_hours
            else:
                journey_stats['abd_logged_out_before_hours'] = 0
                journey_stats['abd_logged_out_after_hours'] = 0
                journey_stats['abd_logged_out_during_hours'] = 0
        else:
            journey_stats['abd_agent_logged_out'] = 0
            journey_stats['abd_agent_logged_in'] = 0
            journey_stats['abd_logged_out_before_hours'] = 0
            journey_stats['abd_logged_out_after_hours'] = 0
            journey_stats['abd_logged_out_during_hours'] = 0
            
        # Polling Attempts Analysis
        if 'Polling Attempts' in abandoned_df.columns:
            journey_stats['abd_zero_polling'] = len(abandoned_df[abandoned_df['Polling Attempts'] == 0])
        else:
            journey_stats['abd_zero_polling'] = 0
    else:
        journey_stats['abd_agent_logged_out'] = 0
        journey_stats['abd_agent_logged_in'] = 0
        journey_stats['abd_zero_polling'] = 0
        
    return journey_stats

def analyze_out_of_hours(main_df, abandoned_df):
    """Analyze calls received outside operating hours with time breakdowns."""
    import pandas as pd
    
    # Combine all calls for OOH analysis
    all_calls = []
    
    if not main_df.empty:
        main_calls = main_df[['call_start']].copy()
        all_calls.append(main_calls)
    
    if not abandoned_df.empty:
        abd_calls = abandoned_df[['Call Time']].copy()
        abd_calls.columns = ['call_start']
        all_calls.append(abd_calls)
    
    if not all_calls:
        return {'ooh_total': 0, 'ooh_before_opening': 0, 'ooh_after_closing': 0}
    
    combined = pd.concat(all_calls, ignore_index=True)
    combined['call_start'] = pd.to_datetime(combined['call_start'])
    combined['hour'] = combined['call_start'].dt.hour
    combined['day_of_week'] = combined['call_start'].dt.dayofweek
    
    # Define operating hours check
    def is_out_of_hours(row):
        day = row['day_of_week']
        hour = row['hour']
        # Mon-Fri (0-4): 8am-8pm
        if day <= 4:
            return hour < 8 or hour >= 20
        # Sat (5): 8am-6pm
        elif day == 5:
            return hour < 8 or hour >= 18
        # Sun (6): 10am-4pm
        elif day == 6:
            return hour < 10 or hour >= 16
        return False
    
    def categorize_ooh(row):
        day = row['day_of_week']
        hour = row['hour']
        if day <= 4:  # Mon-Fri
            return 'before' if hour < 8 else 'after'
        elif day == 5:  # Sat
            return 'before' if hour < 8 else 'after'
        elif day == 6:  # Sun
            return 'before' if hour < 10 else 'after'
        return 'during'
    
    combined['is_ooh'] = combined.apply(is_out_of_hours, axis=1)
    combined['ooh_category'] = combined.apply(categorize_ooh, axis=1)
    
    ooh_calls = combined[combined['is_ooh']]
    
    return {
        'ooh_total': len(ooh_calls),
        'ooh_before_opening': len(ooh_calls[ooh_calls['ooh_category'] == 'before']),
        'ooh_after_closing': len(ooh_calls[ooh_calls['ooh_category'] == 'after'])
    }

def analyze_calls(data_dir='data'):
    """Main analysis function. Loads all CallLog files in data_dir."""
    # 1. Clean and Load Data (Multiple Files)
    print("Cleaning and loading main call logs...")
    files = glob.glob(os.path.join(data_dir, 'CallLogLastWeek_*.csv'))
    dfs = []
    for f in files:
        print(f"Processing {f}...")
        try:
            cleaned = run_cleaning(f)
            dfs.append(cleaned.call_level_df)
        except Exception as e:
            print(f"Error processing {f}: {e}")
            
    if not dfs:
        print("No main call logs found!")
        return {}
        
    df = pd.concat(dfs, ignore_index=True)
    # Deduplicate Main Log (in case of file overlap)
    df = df.drop_duplicates(subset=['Call ID'])
    print(f"Total Main Log Calls (Unique): {len(df)}")

    # RE-CALCULATE WEEKS based on GLOBAL max date
    # This is necessary because cleaning.py calculates weeks per-file
    max_date = df['call_start'].max()
    print(f"Global Max Date: {max_date}")
    
    # Week 1: From (max_date - 7 days) up to and including max_date
    week1_start = max_date - pd.Timedelta(days=7)
    week1_end = max_date
    
    # Week 2: 7 days before Week 1
    week2_start = week1_start - pd.Timedelta(days=7)
    week2_end = week1_start
    
    def assign_week_global(dt):
        if dt > week1_start and dt <= week1_end:
            return 1
        elif dt > week2_start and dt <= week2_end:
            return 2
        else:
            return 3
            
    df['week'] = df['call_start'].apply(assign_week_global)
    print("Re-calculated weeks based on global max date")
    print(f"Week distribution: {df['week'].value_counts().to_dict()}")

    # 2. Save to Database
    save_to_database(df)
    
    # 3. Analyze Abandoned Calls (Load first to combine metrics)
    print("Analyzing abandoned calls files...")
    abandoned_df = load_abandoned_calls()
    
    # 3a. Assign customer type to abandoned_df BEFORE calculating weekly metrics
    if not abandoned_df.empty:
        # Get set of known trade numbers from main log (cleaned)
        # Filter out anonymous and empty strings
        trade_numbers = set(
            df[df['customer_type'] == 'trade']['from_number']
            .apply(clean_phone_for_match)
            .unique()
        )
        if 'anonymous' in trade_numbers: trade_numbers.remove('anonymous')
        if '' in trade_numbers: trade_numbers.remove('')
        
        # Apply mapping to abandoned calls (cleaning caller ID first)
        abandoned_df['customer_type'] = abandoned_df['Caller ID'].apply(
            lambda x: 'trade' if clean_phone_for_match(x) in trade_numbers else 'retail'
        )
        
        # USER REQUEST: Ensure any 'unknown' customer types are classified as retail, not trade
        # This handles edge cases where customer_type might be set to 'unknown' elsewhere
        if 'customer_type' in abandoned_df.columns:
            abandoned_df.loc[abandoned_df['customer_type'].str.lower() == 'unknown', 'customer_type'] = 'retail'
            print(f"Reclassified any 'unknown' abandoned calls to 'retail'")
        
        # Add week calculation to abandoned_df using same logic as cleaning.py
        # Use the max date from MAIN log to align weeks
        abandoned_df['Call Time'] = pd.to_datetime(abandoned_df['Call Time'], errors='coerce')
        
        # Week assignment based on max date (already calculated above)
        def assign_week_abd(dt):
            if dt > week1_start and dt <= week1_end:
                return 1
            elif dt > week2_start and dt <= week2_end:
                return 2
            else:
                return 3
        
        abandoned_df['week'] = abandoned_df['Call Time'].apply(assign_week_abd)
        
        # Keep week_start for compatibility
        abandoned_df['week_start'] = abandoned_df['Call Time'].dt.normalize() - pd.to_timedelta(
            abandoned_df['Call Time'].dt.dayofweek, unit='D'
        )
        
        print(f"Abandoned calls week distribution: {abandoned_df['week'].value_counts().sort_index().to_dict()}")
    
    # 4. Calculate Combined Metrics
    # CRITICAL: Filter to ONLY Week 1 and 2 for ALL calculations
    # This ensures consistency across metrics, plots, and day-of-week charts
    df_week12 = df[df['week'].isin([1, 2])].copy()
    abandoned_week12 = abandoned_df[abandoned_df['week'].isin([1, 2])].copy() if not abandoned_df.empty else pd.DataFrame()
    
    # Store original for export/audit
    df_all_weeks = df.copy()
    abandoned_all_weeks = abandoned_df.copy()
    
    # NOW USE ONLY FILTERED DATA FOR CALCULATIONS
    main_count = len(df_week12)
    abandoned_count = len(abandoned_week12)
    total_calls = main_count + abandoned_count
    
    answered = df_week12['is_answered'].sum()
    abandonment_rate = (abandoned_count / total_calls * 100) if total_calls > 0 else 0
    
    # HISTORICAL CONSISTENCY FIX: Use date-based filtering, not mixed weeks
    # This Week and Last Week use the SAME date range for ALL metrics (Main + Abandoned)
    # No overlapping dates between weeks
    
    # BUG FIX: Normalize max_date to DATE ONLY (midnight) to avoid excluding calls
    # before max_date's time component on boundary days.
    # e.g. if max_date = 2026-02-08 21:44:22, without normalization:
    #   this_week_start = 2026-02-02 21:44:22 (WRONG - excludes morning calls on Feb 2!)
    # With normalization:
    #   this_week_start = 2026-02-02 00:00:00 (CORRECT - includes all calls on Feb 2)
    max_date_normalized = max_date.normalize()  # Strip time -> midnight
    
    # Define This Week: Most recent 7 days (inclusive of full days)
    this_week_end = max_date_normalized + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)  # End of day (23:59:59)
    this_week_start = max_date_normalized - pd.Timedelta(days=6)  # Start of day (00:00:00), 7 days total
    
    # Define Last Week: Previous 7 days, NO OVERLAP with This Week
    last_week_end = this_week_start - pd.Timedelta(seconds=1)  # End of previous day (23:59:59)
    last_week_start = (this_week_start - pd.Timedelta(days=7))  # Start of day, 7 days total
    
    print(f"\nThis Week Date Range: {this_week_start.date()} to {max_date_normalized.date()}")
    print(f"Last Week Date Range: {last_week_start.date()} to {(this_week_start - pd.Timedelta(days=1)).date()}")
    
    # Filter Main logs by date range (not week number)
    week1 = df_week12[(df_week12['call_start'] >= this_week_start) & 
                       (df_week12['call_start'] <= this_week_end)].copy()
    week2 = df_week12[(df_week12['call_start'] >= last_week_start) & 
                       (df_week12['call_start'] <= last_week_end)].copy()
    
    print(f"\nThis Week Main Calls: {len(week1)}")
    print(f"Last Week Main Calls: {len(week2)}")
    
    # Calculate retail/trade breakdowns
    week1_retail = week1[week1['customer_type'] == 'retail']
    week1_trade = week1[week1['customer_type'] == 'trade']
    week2_retail = week2[week2['customer_type'] == 'retail']
    week2_trade = week2[week2['customer_type'] == 'trade']
    
    # Filter Abandoned logs by SAME date ranges (not mixed!)
    if not abandoned_week12.empty:
        week1_abd = abandoned_week12[(abandoned_week12['Call Time'] >= this_week_start) & 
                                      (abandoned_week12['Call Time'] <= this_week_end)].copy()
        week2_abd = abandoned_week12[(abandoned_week12['Call Time'] >= last_week_start) & 
                                      (abandoned_week12['Call Time'] <= last_week_end)].copy()
        
        print(f"This Week Abandoned Calls: {len(week1_abd)}")
        print(f"Last Week Abandoned Calls: {len(week2_abd)}")
        
        week1_retail_abd = len(week1_abd[week1_abd['customer_type'] == 'retail'])
        week1_trade_abd = len(week1_abd[week1_abd['customer_type'] == 'trade'])
        week2_retail_abd = len(week2_abd[week2_abd['customer_type'] == 'retail'])
        week2_trade_abd = len(week2_abd[week2_abd['customer_type'] == 'trade'])
        
        # Totals logic needs to match the breakdown above
        # Note: These are abandoned totals
        total_retail_abd = week1_retail_abd + week2_retail_abd
        total_trade_abd = week1_trade_abd + week2_trade_abd
    else:
        week1_retail_abd = week1_trade_abd = week2_retail_abd = week2_trade_abd = 0
        total_retail_abd = total_trade_abd = 0
    
    # Weekly totals (Main + Abandoned)
    week1_calls_total = len(week1) + (len(week1_abd) if not abandoned_week12.empty else 0)
    week2_calls_total = len(week2) + (len(week2_abd) if not abandoned_week12.empty else 0)
    
    metrics = {
        'total_calls': week1_calls_total + week2_calls_total,  # Only Week 1 + Week 2
        'answered_calls': int(answered),
        'abandoned_calls': int(abandoned_count),
        'abandonment_rate': round(abandonment_rate, 1),
        
        # Date ranges for historical tracking
        'this_week_start': this_week_start.strftime('%Y-%m-%d'),
        'this_week_end': this_week_end.strftime('%Y-%m-%d'),
        'last_week_start': last_week_start.strftime('%Y-%m-%d'),
        'last_week_end': last_week_end.strftime('%Y-%m-%d'),
        
        # Weekly totals (Main + Abandoned)
        'week1_calls': week1_calls_total,
        'week2_calls': week2_calls_total,
        
        # Main log only breakdowns -> USER REQUEST: Do NOT include Abandoned in these counts
        # This ensures: Retail (Main) + Trade (Main) + Abandoned = Total
        'week1_retail_calls': len(week1_retail),
        'week1_trade_calls': len(week1_trade),
        'week2_retail_calls': len(week2_retail),
        'week2_trade_calls': len(week2_trade),
        
        # Abandoned breakdowns
        'week1_retail_abandoned': week1_retail_abd,
        'week1_trade_abandoned': week1_trade_abd,
        'week2_retail_abandoned': week2_retail_abd,
        'week2_trade_abandoned': week2_trade_abd,

        'week1_abandoned_total': week1_retail_abd + week1_trade_abd,
        'week2_abandoned_total': week2_retail_abd + week2_trade_abd,

        'retail_abandoned': total_retail_abd,
        'trade_abandoned': total_trade_abd,

        # Combined totals - USER REQUEST: Exclude abandoned from Retail/Trade display metrics
        'week1_retail_total': len(week1_retail),
        'week1_trade_total': len(week1_trade),
        'week2_retail_total': len(week2_retail),
        'week2_trade_total': len(week2_trade),
    }
    
    # Calculate abandonment rates by customer type
    # FIXED: Use filtered data (weeks 1 & 2 only)
    total_retail_main = len(df_week12[df_week12['customer_type'] == 'retail'])
    total_trade_main = len(df_week12[df_week12['customer_type'] == 'trade'])
    
    metrics['retail_abandonment_rate'] = round(
        (total_retail_abd / (total_retail_main + total_retail_abd) * 100) 
        if (total_retail_main + total_retail_abd) > 0 else 0, 1
    )
    
    metrics['trade_abandonment_rate'] = round(
        (total_trade_abd / (total_trade_main + total_trade_abd) * 100) 
        if (total_trade_main + total_trade_abd) > 0 else 0, 1
    )
    
    # Calculate weekly abandonment rates by customer type
    # Week 1
    week1_retail_main = len(week1_retail)
    week1_trade_main = len(week1_trade)
    
    metrics['week1_retail_abandonment_rate'] = round(
        (week1_retail_abd / (week1_retail_main + week1_retail_abd) * 100)
        if (week1_retail_main + week1_retail_abd) > 0 else 0, 1
    )
    
    metrics['week1_trade_abandonment_rate'] = round(
        (week1_trade_abd / (week1_trade_main + week1_trade_abd) * 100)
        if (week1_trade_main + week1_trade_abd) > 0 else 0, 1
    )
    
    # Week 2
    week2_retail_main = len(week2_retail)
    week2_trade_main = len(week2_trade)
    
    metrics['week2_retail_abandonment_rate'] = round(
        (week2_retail_abd / (week2_retail_main + week2_retail_abd) * 100)
        if (week2_retail_main + week2_retail_abd) > 0 else 0, 1
    )
    
    metrics['week2_trade_abandonment_rate'] = round(
        (week2_trade_abd / (week2_trade_main + week2_trade_abd) * 100)
        if (week2_trade_main + week2_trade_abd) > 0 else 0, 1
    )
    
    # Prepare abandoned_with_week for export (already has week and customer_type)
    # Export filtered version (weeks 1 & 2 only for consistency)
    abandoned_with_week = abandoned_week12.copy() if not abandoned_week12.empty else pd.DataFrame()

    # 6. Analyze Journey (Already using filtered week12 data)
    journey_stats = analyze_journey(df_week12, abandoned_week12)
    metrics.update(journey_stats)
    
    # 6b. Extract Abandoned Trade Customers by Week
    abandoned_trade_customers = {'week1': [], 'week2': []}
    if not abandoned_week12.empty:
        # Load trade customer names if available
        trade_names_path = os.path.join(data_dir, 'trade_customer_numbers.csv')
        trade_names_map = {}
        if os.path.exists(trade_names_path):
            try:
                trade_df = pd.read_csv(trade_names_path)
                if 'phone_number' in trade_df.columns and 'customer_name' in trade_df.columns:
                    for _, row in trade_df.iterrows():
                        cleaned_num = clean_phone_for_match(row['phone_number'])
                        trade_names_map[cleaned_num] = row['customer_name']
            except Exception as e:
                print(f"Could not load trade customer names: {e}")
        
        # Get abandoned trade customers for each week
        for week_num in [1, 2]:
            week_key = f'week{week_num}'
            week_abandoned = abandoned_df[
                (abandoned_df['week'] == week_num) & 
                (abandoned_df['customer_type'] == 'trade')
            ].copy()
            
            if not week_abandoned.empty:
                # Clean phone numbers for matching
                week_abandoned['cleaned_phone'] = week_abandoned['Caller ID'].apply(
                    lambda x: clean_phone_for_match(x)
                )
                
                # Process each abandoned call individually (don't group)
                for _, row in week_abandoned.iterrows():
                    phone = row['Caller ID']
                    cleaned = row['cleaned_phone']
                    call_time = row['Call Time']
                    
                    # Get customer name if available
                    customer_name = trade_names_map.get(cleaned, None)
                    
                    # USER REQUEST: Skip entries with Unknown/missing names
                    # These are not verified trade customers and should be treated as retail
                    if customer_name is None or customer_name.strip().upper() == 'UNKNOWN':
                        continue  # Skip - not a verified trade customer
                    
                    customer_name = customer_name.upper()
                    
                    # Format the call time
                    if pd.notnull(call_time):
                        if isinstance(call_time, str):
                            call_time = pd.to_datetime(call_time, errors='coerce')
                        call_time_str = call_time.strftime('%d/%m/%Y %H:%M') if pd.notnull(call_time) else 'N/A'
                        sort_time = call_time if pd.notnull(call_time) else pd.Timestamp.min
                    else:
                        call_time_str = 'N/A'
                        sort_time = pd.Timestamp.min
                    
                    abandoned_trade_customers[week_key].append({
                        'name': customer_name,
                        'phone': phone,
                        'call_time': call_time_str,
                        'sort_time': sort_time
                    })
                
                # Sort by call time (most recent first)
                abandoned_trade_customers[week_key].sort(
                    key=lambda x: x['sort_time'], 
                    reverse=True
                )
                
                # Remove sort_time field before returning (not needed in template)
                for item in abandoned_trade_customers[week_key]:
                    del item['sort_time']
    
    # 6a. Analyze Out of Hours (Enhanced) - Week 1 and Week 2 only
    ooh_stats = analyze_out_of_hours(df_week12, abandoned_week12)
    metrics.update(ooh_stats)
    
    # 7. Generate Plots & Get Bottom-Up Metrics
    plots, plot_metrics = generate_plots(df, abandoned_df)
    
    # 7b. OVERWRITE metrics with plot-derived metrics ("Bottom Up" approach)
    # This guarantees that the data cards match the plots exactly
    metrics.update(plot_metrics)
    
    # Re-calculate Total Calls metric for consistency
    # (Retail Main + Trade Main + Abandoned Total)
    metrics['total_calls'] = metrics['week1_calls'] + metrics['week2_calls']
    
    # 8. Export Datasets for Download
    print("Exporting datasets for download...")
    try:
        # Export cleaned call logs
        df.to_csv('reports/call_logs_cleaned.csv', index=False)
        print("Exported cleaned call logs to reports/call_logs_cleaned.csv")
        
        # Export original/raw call logs (combine all raw files)
        raw_files = glob.glob(os.path.join(data_dir, 'CallLogLastWeek_*.csv'))
        raw_dfs = []
        for f in raw_files:
            raw_dfs.append(pd.read_csv(f))
        raw_combined = pd.concat(raw_dfs, ignore_index=True)
        raw_combined.to_csv('reports/call_logs_original.csv', index=False)
        print("Exported original call logs to reports/call_logs_original.csv")
        
        # Export cleaned abandoned logs
        if not abandoned_df.empty:
            abandoned_df.to_csv('reports/abandoned_logs_cleaned.csv', index=False)
            print("Exported cleaned abandoned logs to reports/abandoned_logs_cleaned.csv")
        
        # Export original abandoned logs
        abd_files = glob.glob(os.path.join(data_dir, 'AbandonedCalls*.csv'))
        if abd_files:
            abd_raw_dfs = []
            for f in abd_files:
                abd_raw_dfs.append(pd.read_csv(f))
            abd_raw_combined = pd.concat(abd_raw_dfs, ignore_index=True)
            abd_raw_combined.to_csv('reports/abandoned_logs_original.csv', index=False)
            print("Exported original abandoned logs to reports/abandoned_logs_original.csv")
    except Exception as e:
        print(f"Error exporting datasets: {e}")
    
    # 10. Generate Narrative
    # Calculate week date ranges for narrative
    week1_start_date = (max_date - pd.Timedelta(days=6)).strftime('%d/%m/%Y')
    week1_end_date = max_date.strftime('%d/%m/%Y')
    week2_start_date = (max_date - pd.Timedelta(days=13)).strftime('%d/%m/%Y')
    week2_end_date = (max_date - pd.Timedelta(days=7)).strftime('%d/%m/%Y')
    
    narrative = f"""
    Received a total of <b>{metrics['total_calls']:,}</b> calls across This Week and Last Week.
    <br><br>
    <b>This Week</b> ({week1_start_date} to {week1_end_date}): Received {metrics['week1_calls']:,} calls total.
    <br>
    - Retail: {metrics.get('week1_retail_total', 0):,} calls
    <br>
    - Trade: {metrics.get('week1_trade_total', 0):,} calls
    <br>
    - Abandoned: {metrics.get('week1_retail_abandoned', 0) + metrics.get('week1_trade_abandoned', 0):,} calls 
      (Retail: {metrics.get('week1_retail_abandoned', 0):,}, Trade: {metrics.get('week1_trade_abandoned', 0):,})
    <br><br>
    <b>Last Week</b> ({week2_start_date} to {week2_end_date}): Received {metrics['week2_calls']:,} calls total.
    <br>
    - Retail: {metrics.get('week2_retail_total', 0):,} calls
    <br>
    - Trade: {metrics.get('week2_trade_total', 0):,} calls
    <br>
    - Abandoned: {metrics.get('week2_retail_abandoned', 0) + metrics.get('week2_trade_abandoned', 0):,} calls
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
    - <b>Agents Logged Out:</b> {metrics['abd_agent_logged_out']:,} abandoned calls when no agents were logged in
      <br>&nbsp;&nbsp;&nbsp;&nbsp;(Before Opening: {metrics.get('abd_logged_out_before_hours', 0):,}, 
      During Business Hours: {metrics.get('abd_logged_out_during_hours', 0):,}, 
      After Closing: {metrics.get('abd_logged_out_after_hours', 0):,})
    <br>
    - <b>Zero Polling:</b> {metrics['abd_zero_polling']:,} abandoned calls with 0 polling attempts (system couldn't reach any agent - typically when all agents busy/offline).
    """
    
    return {
        'metrics': metrics,
        'plots': plots,
        'narrative': narrative,
        'raw_data': df_week12,  # Export filtered data (weeks 1 & 2 only)
        'abandoned_logs': abandoned_with_week,
        'raw_data_all_weeks': df_all_weeks,  # Keep full data for audit
        'abandoned_all_weeks': abandoned_all_weeks,  # Keep full data for audit
        'max_date': max_date.strftime('%d/%m/%Y') if pd.notnull(max_date) else "N/A",
        'max_date_obj': max_date if pd.notnull(max_date) else None,  # Add datetime object for filename
        'abandoned_trade_customers': abandoned_trade_customers
    }

if __name__ == "__main__":
    results = analyze_calls('data')
    print("Analysis complete.")
    print(results['metrics'])
