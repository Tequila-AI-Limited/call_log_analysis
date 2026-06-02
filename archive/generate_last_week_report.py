
import os
from jinja2 import Environment, FileSystemLoader
from call_log_analyzer import analyze_calls, save_to_database, load_abandoned_calls, generate_plots, analyze_journey, analyze_out_of_hours
import pandas as pd
import glob
from cleaning import run_cleaning
from datetime import datetime

def generate_last_week_report():
    """
    Generate report explicitly for the PREVIOUS week (Week 6: Jan 26 - Feb 1).
    This simulates what the report would have looked like if run last week.
    """
    print("Generating report for Last Week (Jan 26 - Feb 1)...")
    
    # Target Date: Feb 1st 2026 (Sunday of that week)
    target_max_date = pd.Timestamp("2026-02-01")
    
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    
    # 1. Load Data (Using same logic as analyze_calls but filtering for date)
    print("Cleaning and loading main call logs...")
    files = glob.glob(os.path.join(data_dir, 'CallLogLastWeek_*.csv'))
    dfs = []
    
    # Filter files? Or just load all and filter by date?
    # Safer to load all and filter by date to ensure we catch everything.
    for f in files:
        try:
            cleaned = run_cleaning(f)
            dfs.append(cleaned.call_level_df)
        except Exception as e:
            print(f"Error processing {f}: {e}")
            
    if not dfs:
        print("No main call logs found!")
        return

    df = pd.concat(dfs, ignore_index=True)
    df = df.drop_duplicates(subset=['Call ID'])
    
    # FILTER DATE: Up to target_max_date
    # We want "This Week" to be Jan 26 - Feb 1
    # We want "Last Week" to be Jan 19 - Jan 25
    df = df[df['call_start'] <= target_max_date + pd.Timedelta(days=1)] # Buffer for TZ? just use strict date
    
    # Re-calculate weeks relative to OUR TARGET DATE, not the global max
    week1_start = target_max_date - pd.Timedelta(days=6) # Jan 26
    week1_end = target_max_date # Feb 1
    
    week2_start = week1_start - pd.Timedelta(days=7) # Jan 19
    week2_end = week1_start - pd.Timedelta(days=1) # Jan 25
    
    print(f"Report Target Date: {target_max_date.date()}")
    print(f"This Week (W1): {week1_start.date()} to {week1_end.date()}")
    print(f"Last Week (W2): {week2_start.date()} to {week2_end.date()}")
    
    def assign_week_target(dt):
        if pd.isnull(dt):
            return 3
            
        # Get date object safely
        if hasattr(dt, 'date'):
            d = dt.date()
        elif isinstance(dt, datetime):
            d = dt.date()
        else:
            d = dt
            
        # Ensure we have datetime.date objects for comparison anchors
        w1_s = week1_start.date() 
        w1_e = week1_end.date()
        w2_s = week2_start.date()
        w2_e = week2_end.date()
        
        try:
            # Check Week 1
            if d >= w1_s and d <= w1_e:
                return 1
            # Check Week 2
            elif d >= w2_s and d <= w2_e:
                return 2
            else:
                return 3
        except Exception:
            # If any comparison fails (e.g. incompatible types), return 3
            return 3
            
    df['week'] = df['call_start'].apply(assign_week_target)
    
    # 2. Abandoned Calls
    abandoned_df = load_abandoned_calls()
    
    if not abandoned_df.empty:
        # Assign customer type (Trade vs Retail) - reusing logic from analyzer
        # Simplified for brevity - copying core logic
        def clean_phone_for_match(phone):
            s = str(phone).strip()
            s = s.replace(' ', '').replace('-', '').replace('.', '').replace('(', '').replace(')', '')
            if s.startswith('+353'): s = s[4:]
            elif s.startswith('00353'): s = s[5:]
            elif s.startswith('353'): s = s[3:]
            if s.startswith('0'): s = s[1:]
            return s

        trade_numbers = set(
            df[df['customer_type'] == 'trade']['from_number']
            .apply(clean_phone_for_match)
            .unique()
        )
        if 'anonymous' in trade_numbers: trade_numbers.remove('anonymous')
        if '' in trade_numbers: trade_numbers.remove('')
        
        abandoned_df['customer_type'] = abandoned_df['Caller ID'].apply(
            lambda x: 'trade' if clean_phone_for_match(x) in trade_numbers else 'retail'
        )
        if 'customer_type' in abandoned_df.columns:
            abandoned_df.loc[abandoned_df['customer_type'].str.lower() == 'unknown', 'customer_type'] = 'retail'

        abandoned_df['Call Time'] = pd.to_datetime(abandoned_df['Call Time'], errors='coerce')
        abandoned_df['week'] = abandoned_df['Call Time'].apply(assign_week_target)
        
        # Keep week_start
        abandoned_df['week_start'] = abandoned_df['Call Time'].dt.normalize() - pd.to_timedelta(
            abandoned_df['Call Time'].dt.dayofweek, unit='D'
        )


    # 4. Metrics Calculation (Weeks 1 & 2 only)
    df_week12 = df[df['week'].isin([1, 2])].copy()
    abandoned_week12 = abandoned_df[abandoned_df['week'].isin([1, 2])].copy() if not abandoned_df.empty else pd.DataFrame()
    
    # Calculate Metrics Dictionary
    # Define date lists for boolean indexing
    w1_dates = [d.date() for d in pd.date_range(week1_start, week1_end)]
    w2_dates = [d.date() for d in pd.date_range(week2_start, week2_end)]
    
    # Filter Main
    week1 = df[df['call_start'].dt.date.isin(w1_dates)]
    week2 = df[df['call_start'].dt.date.isin(w2_dates)]
    
    # Filter Abd
    if not abandoned_df.empty:
        week1_abd = abandoned_df[abandoned_df['Call Time'].dt.date.isin(w1_dates)]
        week2_abd = abandoned_df[abandoned_df['Call Time'].dt.date.isin(w2_dates)]
    else:
        week1_abd = pd.DataFrame()
        week2_abd = pd.DataFrame()

    # Build Metrics Dict
    metrics = {
        'total_calls': len(week1) + len(week1_abd) + len(week2) + len(week2_abd),
        
        # Totals
        'week1_calls': len(week1) + len(week1_abd),
        'week2_calls': len(week2) + len(week2_abd),
        
        # Main Breakdowns (Retail/Trade) - EXCLUDING Abandoned
        'week1_retail_calls': len(week1[week1['customer_type'] == 'retail']),
        'week1_trade_calls': len(week1[week1['customer_type'] == 'trade']),
        'week2_retail_calls': len(week2[week2['customer_type'] == 'retail']),
        'week2_trade_calls': len(week2[week2['customer_type'] == 'trade']),
        
        # Abandoned Breakdowns
        'week1_retail_abandoned': len(week1_abd[week1_abd['customer_type'] == 'retail']) if not week1_abd.empty else 0,
        'week1_trade_abandoned': len(week1_abd[week1_abd['customer_type'] == 'trade']) if not week1_abd.empty else 0,
        'week2_retail_abandoned': len(week2_abd[week2_abd['customer_type'] == 'retail']) if not week2_abd.empty else 0,
        'week2_trade_abandoned': len(week2_abd[week2_abd['customer_type'] == 'trade']) if not week2_abd.empty else 0,
        
        'this_week_start': week1_start.strftime('%Y-%m-%d'),
        'this_week_end': week1_end.strftime('%Y-%m-%d'),
        'last_week_start': week2_start.strftime('%Y-%m-%d'),
        'last_week_end': week2_end.strftime('%Y-%m-%d'),
    }
    
    # Aliases for Template Compatibility
    metrics['week1_retail_total'] = metrics['week1_retail_calls']
    metrics['week1_trade_total'] = metrics['week1_trade_calls']
    metrics['week2_retail_total'] = metrics['week2_retail_calls']
    metrics['week2_trade_total'] = metrics['week2_trade_calls']
    
    # Add totals for display
    metrics['week1_retail_total'] = metrics['week1_retail_calls']
    metrics['week1_trade_total'] = metrics['week1_trade_calls'] 
    metrics['week2_retail_total'] = metrics['week2_retail_calls']
    metrics['week2_trade_total'] = metrics['week2_trade_calls']
    
    # Rates
    def safe_rate(num, denom):
        return round((num / denom * 100), 1) if denom > 0 else 0
        
    metrics['week1_retail_abandonment_rate'] = safe_rate(metrics['week1_retail_abandoned'], metrics['week1_retail_total'] + metrics['week1_retail_abandoned'])
    metrics['week1_trade_abandonment_rate'] = safe_rate(metrics['week1_trade_abandoned'], metrics['week1_trade_total'] + metrics['week1_trade_abandoned'])
    metrics['week2_retail_abandonment_rate'] = safe_rate(metrics['week2_retail_abandoned'], metrics['week2_retail_total'] + metrics['week2_retail_abandoned'])
    metrics['week2_trade_abandonment_rate'] = safe_rate(metrics['week2_trade_abandoned'], metrics['week2_trade_total'] + metrics['week2_trade_abandoned'])

    # Journey & OOH
    journey_stats = analyze_journey(df_week12, abandoned_week12)
    metrics.update(journey_stats)
    ooh_stats = analyze_out_of_hours(df_week12, abandoned_week12)
    metrics.update(ooh_stats)

    # Plots
    print("Generating plots...")
    # Plots function expects 'week' column to be 1 or 2, which we set earlier
    plots, plot_metrics = generate_plots(df, abandoned_df)
    # We can perform the same "Bottom Up" consistency check if we want, but for now let's trust our metric calc
    # Actually, let's update with plot metrics to be safe as that's what the main script does
    metrics.update(plot_metrics)
    # Re-sum total
    metrics['total_calls'] = metrics['week1_calls'] + metrics['week2_calls']

    # Narrative
    # Dynamic narrative that uses calculated metrics to ensure consistency
    narrative = f"""
    Received a total of <b>{metrics['total_calls']:,}</b> calls across This Week and Last Week.
    <br><br>
    <b>This Week</b> ({week1_start.strftime('%d/%m/%Y')} to {week1_end.strftime('%d/%m/%Y')}): Received {metrics['week1_calls']:,} calls total.
    <br>
    - Retail: {metrics.get('week1_retail_total', 0):,} calls
    <br>
    - Trade: {metrics.get('week1_trade_total', 0):,} calls
    <br>
    - Abandoned: {metrics.get('week1_retail_abandoned', 0) + metrics.get('week1_trade_abandoned', 0):,} calls 
    <br><br>
    <b>Last Week</b> ({week2_start.strftime('%d/%m/%Y')} to {week2_end.strftime('%d/%m/%Y')}): Received {metrics['week2_calls']:,} calls total.
    <br>
    - Retail: {metrics.get('week2_retail_total', 0):,} calls
    <br>
    - Trade: {metrics.get('week2_trade_total', 0):,} calls
    <br>
    - Abandoned: {metrics.get('week2_retail_abandoned', 0) + metrics.get('week2_trade_abandoned', 0):,} calls 
    """
    
    # Render
    env = Environment(loader=FileSystemLoader('templates'))
    template = env.get_template('call_report.html.j2')
    
    html_output = template.render(
        metrics=metrics,
        plots=plots,
        narrative=narrative,
        raw_data=df_week12,
        abandoned_logs=abandoned_week12,
        max_date=target_max_date.strftime('%d/%m/%Y'),
        abandoned_trade_customers={'week1': [], 'week2': []} # Empty for this quick report
    )
    
    output_filename = 'reports/call_report_01_02_2026.html'
    with open(output_filename, 'w', encoding='utf-8') as f:
        f.write(html_output)
        
    print(f"Generated historical report: {output_filename}")
    print(f"This Week (W1): {metrics['week1_calls']}")
    print(f"Last Week (W2): {metrics['week2_calls']}")

if __name__ == "__main__":
    generate_last_week_report()
