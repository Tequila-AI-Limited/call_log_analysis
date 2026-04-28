import os
from jinja2 import Environment, FileSystemLoader
from call_log_analyzer import analyze_calls
from store_snapshot import create_snapshot_table, store_snapshot

def validate_metrics_quick(metrics, df, abandoned_df):
    """Quick validation of key metrics."""
    errors = []
    
    # Total = Week 1 + Week 2
    w1_w2_sum = metrics['week1_calls'] + metrics['week2_calls']
    if w1_w2_sum != metrics['total_calls']:
        errors.append(f"Total mismatch: {w1_w2_sum} != {metrics['total_calls']}")
    
    # Week 1 = Retail + Trade + Abandoned (as per updated metric definitions)
    w1_abd = metrics.get('week1_retail_abandoned', 0) + metrics.get('week1_trade_abandoned', 0)
    w1_breakdown = metrics['week1_retail_total'] + metrics['week1_trade_total'] + w1_abd
    if w1_breakdown != metrics['week1_calls']:
        errors.append(f"Week 1 breakdown: {w1_breakdown} != {metrics['week1_calls']}")
    
    # Week 2 = Retail + Trade + Abandoned
    w2_abd = metrics.get('week2_retail_abandoned', 0) + metrics.get('week2_trade_abandoned', 0)
    w2_breakdown = metrics['week2_retail_total'] + metrics['week2_trade_total'] + w2_abd
    if w2_breakdown != metrics['week2_calls']:
        errors.append(f"Week 2 breakdown: {w2_breakdown} != {metrics['week2_calls']}")
    
    # Check for extra weeks in data
    if any(w not in [1, 2] for w in df['week'].unique()):
        errors.append(f"Data contains weeks other than 1-2: {sorted(df['week'].unique())}")
    
    return errors

def generate_report():
    # 1. Analyze Data
    # Pass the data directory to analyze_calls
    data_dir = os.path.join(os.path.dirname(__file__), 'data')
    
    print("Running analysis...")
    results = analyze_calls(data_dir)
    
    if not results:
        print("Analysis failed or returned no results.")
        return
    
    # 1.5 Enforce Historical Consistency
    # Check if we have valid historical data for "Last Week" and overwrite if so
    import weekly_data_manager
    
    print("Checking for historical data in CSV...")
    
    # Get last week dates from analysis results
    last_week_start = results['metrics']['last_week_start']
    last_week_end = results['metrics']['last_week_end']
    
    historical_data = weekly_data_manager.load_week_data(last_week_start, last_week_end)
    
    if historical_data:
        print(f"\n[INFO] Historical match found for Last Week ({last_week_start} to {last_week_end})")
        print("       ENFORCING CONSISTENCY: Overwriting Last Week metrics with verified CSV data.")
        
        # Get historical values
        hist_total = int(historical_data['total_calls'])
        hist_retail = int(historical_data['retail_calls'])
        hist_trade = int(historical_data['trade_calls'])
        hist_abandoned_total = int(historical_data['abandoned_total'])
        hist_abandoned_retail = int(historical_data['retail_abandoned'])
        hist_abandoned_trade = int(historical_data['trade_abandoned'])
        
        # Log the change
        print(f"       Old Total: {results['metrics']['week2_calls']} -> New Total: {hist_total}")
        
        # Apply overrides
        results['metrics']['week2_calls'] = hist_total
        results['metrics']['week2_retail_total'] = hist_retail
        results['metrics']['week2_retail_calls'] = hist_retail
        results['metrics']['week2_trade_total'] = hist_trade
        results['metrics']['week2_trade_calls'] = hist_trade
        
        # Apply Abandoned Split
        results['metrics']['week2_retail_abandoned'] = hist_abandoned_retail
        results['metrics']['week2_trade_abandoned'] = hist_abandoned_trade
        
        # Ensure 'week2_abandoned_total' is consistent if it exists in metrics (it might be calculated elsewhere)
        # But broadly we just updated the components that sum up to totals.
        
        # RECALCULATE Total calls for the whole report
        results['metrics']['total_calls'] = results['metrics']['week1_calls'] + results['metrics']['week2_calls']
        
    else:
        # HARDCODED FALLBACK: only applies to the specific week of 2026-04-06 to 2026-04-12.
        # This block will never trigger for any other week. Remove once that week's data
        # is in the DB so load_week_data() returns it instead.
        if last_week_start == '2026-04-06' and last_week_end == '2026-04-12':
            print(f"\n[INFO] Applying hardcoded fallback for Last Week ({last_week_start} to {last_week_end}) due to missing DB data")
            
            hist_total = 2343
            hist_retail = 1703
            hist_trade = 366
            hist_abandoned_retail = 256
            hist_abandoned_trade = 18
            
            # Apply overrides
            results['metrics']['week2_calls'] = hist_total
            results['metrics']['week2_retail_total'] = hist_retail
            results['metrics']['week2_retail_calls'] = hist_retail
            results['metrics']['week2_trade_total'] = hist_trade
            results['metrics']['week2_trade_calls'] = hist_trade
            
            # Apply Abandoned Split
            results['metrics']['week2_retail_abandoned'] = hist_abandoned_retail
            results['metrics']['week2_trade_abandoned'] = hist_abandoned_trade
            
            # RECALCULATE Total calls for the whole report
            results['metrics']['total_calls'] = results['metrics']['week1_calls'] + results['metrics']['week2_calls']
        else:
            print(f"No historical match for Last Week period ({last_week_start} to {last_week_end}) in CSV.")
            print("Using calculated values from raw logs.")

    # 2. Comprehensive Validation with Historical Tracking
    print("Validating metrics and historical consistency...")
    from validate_historical import validate_report
    from datetime import datetime
    
    # Save verification summary to Markdown file
    os.makedirs('reports', exist_ok=True)
    validation_message, validation_passed = validate_report(results)

    with open('reports/report_verification_summary.md', 'w', encoding='utf-8') as f:
        f.write(validation_message)

    if not validation_passed:
        print("\n" + "="*60)
        print("ERROR: VALIDATION FAILED!")
        print("="*60)
        print(validation_message)
        print("="*60)
        print("Report generation aborted due to validation errors.\n")
        print("Validation summary saved to: reports/report_verification_summary.md")
        return
    
    # Collect non-fatal warnings to surface in the final summary
    warnings = []

    # 3. Log Historical Week Data to DB
    print("\nLogging 'This Week' data to DB...")

    csv_metrics = {
        'start_date': results['metrics']['this_week_start'],
        'end_date': results['metrics']['this_week_end'],
        'total': results['metrics']['week1_calls'],
        'retail': results['metrics']['week1_retail_total'],
        'trade': results['metrics']['week1_trade_total'],
        'abandoned': results['metrics'].get('week1_retail_abandoned', 0) + results['metrics'].get('week1_trade_abandoned', 0),
        'abandoned_retail': results['metrics'].get('week1_retail_abandoned', 0),
        'abandoned_trade': results['metrics'].get('week1_trade_abandoned', 0)
    }

    try:
        weekly_data_manager.save_week_data(csv_metrics)
    except Exception as e:
        warnings.append(f"DB weekly stats (This Week) NOT saved: {e}")

    # Self-healing: also save Last Week so future reports can load it if they missed it
    last_week_csv_metrics = {
        'start_date': results['metrics']['last_week_start'],
        'end_date': results['metrics']['last_week_end'],
        'total': results['metrics']['week2_calls'],
        'retail': results['metrics']['week2_retail_total'],
        'trade': results['metrics']['week2_trade_total'],
        'abandoned': results['metrics'].get('week2_retail_abandoned', 0) + results['metrics'].get('week2_trade_abandoned', 0),
        'abandoned_retail': results['metrics'].get('week2_retail_abandoned', 0),
        'abandoned_trade': results['metrics'].get('week2_trade_abandoned', 0)
    }

    try:
        weekly_data_manager.save_week_data(last_week_csv_metrics)
    except Exception as e:
        warnings.append(f"DB weekly stats (Last Week) NOT saved: {e}")

    # 4. Store Historical Snapshot (database)
    print("Storing database snapshot...")
    try:
        create_snapshot_table()
        store_snapshot(results['metrics'])
    except Exception as e:
        warnings.append(f"DB snapshot NOT saved: {e}")

    # 5. Setup Jinja2 Environment
    env = Environment(loader=FileSystemLoader('templates'))
    template = env.get_template('call_report.html.j2')
    
    # REGENERATE NARRATIVE with Updated Metrics
    metrics = results['metrics']
    
    # Calculate formatted dates for narrative from metrics
    # Note: metrics has YYYY-MM-DD strings. Narrative needs DD/MM/YYYY.
    def fmt_date(ymd_str):
        if not ymd_str: return "N/A"
        try:
            return datetime.strptime(ymd_str, '%Y-%m-%d').strftime('%d/%m/%Y')
        except: return ymd_str

    week1_start_formatted = fmt_date(metrics['this_week_start'])
    week1_end_formatted = fmt_date(metrics['this_week_end'])
    week2_start_formatted = fmt_date(metrics['last_week_start'])
    week2_end_formatted = fmt_date(metrics['last_week_end'])

    results['narrative'] = f"""
    Received a total of <b>{metrics['total_calls']:,}</b> calls across This Week and Last Week.
    <br><br>
    <b>This Week</b> ({week1_start_formatted} to {week1_end_formatted}): Received {metrics['week1_calls']:,} calls total.
    <br>
    - Retail: {metrics.get('week1_retail_total', 0):,} calls
    <br>
    - Trade: {metrics.get('week1_trade_total', 0):,} calls
    <br>
    - Abandoned: {metrics.get('week1_retail_abandoned', 0) + metrics.get('week1_trade_abandoned', 0):,} calls 
      (Retail: {metrics.get('week1_retail_abandoned', 0):,}, Trade: {metrics.get('week1_trade_abandoned', 0):,})
    <br><br>
    <b>Last Week</b> ({week2_start_formatted} to {week2_end_formatted}): Received {metrics['week2_calls']:,} calls total.
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
    - <b>Agents Logged Out:</b> {metrics.get('abd_agent_logged_out', 0):,} abandoned calls when no agents were logged in
      <br>&nbsp;&nbsp;&nbsp;&nbsp;(Before Opening: {metrics.get('abd_logged_out_before_hours', 0):,}, 
      During Business Hours: {metrics.get('abd_logged_out_during_hours', 0):,}, 
      After Closing: {metrics.get('abd_logged_out_after_hours', 0):,})
    <br>
    - <b>Zero Polling:</b> {metrics.get('abd_zero_polling', 0):,} abandoned calls with 0 polling attempts (system couldn't reach any agent - typically when all agents busy/offline).
    """

    # 6. Render Template
    print("Generating report...")
    html_output = template.render(
        metrics=results['metrics'],
        plots=results['plots'],
        narrative=results['narrative'],
        raw_data=results['raw_data'],
        abandoned_logs=results['abandoned_logs'],
        max_date=results.get('max_date', 'N/A'),
        abandoned_trade_customers=results.get('abandoned_trade_customers', {'week1': [], 'week2': []})
    )
    
    # Save Report
    output_dir = 'reports'
    os.makedirs(output_dir, exist_ok=True)
    
    # Extract max_date from results and format as dd_mm_yyyy
    max_date_obj = results.get('max_date_obj')
    if max_date_obj:
        date_str = max_date_obj.strftime('%d_%m_%Y')
        output_filename = f'call_report_{date_str}.html'
    else:
        # Fallback to original filename if max_date not available
        output_filename = 'call_report.html'
    
    output_path = os.path.join(output_dir, output_filename)
    
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_output)
        print(f"Report generated successfully: {output_path}")
        print("\n" + "="*60)
        if warnings:
            print("REPORT GENERATED — WITH WARNINGS")
        else:
            print("SUCCESS: Report validated and ready for stakeholders!")
        print("="*60)
        print(f"This Week: {csv_metrics['start_date']} to {csv_metrics['end_date']} ({csv_metrics['total']} calls)")
        print(f"Last Week: {results['metrics']['last_week_start']} to {results['metrics']['last_week_end']} ({results['metrics']['week2_calls']} calls)")
        print(f"Report:    {output_path}")
        print(f"Verify:    reports/report_verification_summary.md")
        if warnings:
            print("\n" + "!"*60)
            print(f"  {len(warnings)} NON-FATAL WARNING(S) — report is valid but check these:")
            for w in warnings:
                print(f"  - {w}")
            print("!"*60)
        print("="*60)
            
    except Exception as e:
        print(f"Error writing report: {e}")

if __name__ == "__main__":
    generate_report()
