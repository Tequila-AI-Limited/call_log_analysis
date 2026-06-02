# Claude Code Analysis - Call Log Analysis Pipeline

**Date:** 2026-06-02 (last updated)
**Project:** Southside/Omni Call Center Weekly Report Generator
**Stack:** Python 3, Pandas, Plotly, Jinja2, PostgreSQL, Microsoft Graph API

---

## 1. Project Overview

This pipeline ingests raw call log CSVs from a 3CX phone system, cleans and aggregates them, calculates weekly metrics (retail vs trade vs abandoned calls), generates interactive HTML reports, and stores snapshots to a PostgreSQL database. Email ingestion from Microsoft 365 is partially implemented.

### Pipeline Flow

```
Email (3CX noreply@3cx.net)
    -> ingest_email.py (fetch attachments via MS Graph API, convert xlsx -> csv)
    -> data/ folder (CSV files land here)
    -> generate_report.py (orchestrator)
        -> call_log_analyzer.py::analyze_calls()
            -> cleaning.py (parse, classify, aggregate per-file)
            -> concat + deduplicate all files
            -> re-assign weeks globally based on max_date
            -> load & classify abandoned calls
            -> calculate metrics
            -> generate_plots() (Plotly charts only, no metric override)
            -> export CSVs
        -> validate_historical.py (arithmetic + historical checks)
        -> historical_log.py (append to JSON log)
        -> store_snapshot.py (write to report_snapshots + weekly_metrics tables)
        -> Jinja2 render -> reports/call_report.html
    -> cleanup_data.py (archive processed CSVs)
```

---

## 2. File Map

| File | Purpose | Key Functions |
|---|---|---|
| `generate_report.py` | Orchestrator entry point | `generate_report()`, `validate_metrics_quick()` |
| `call_log_analyzer.py` | Core analysis engine | `analyze_calls()`, `generate_plots()`, `load_abandoned_calls()`, `save_to_database()` |
| `cleaning.py` | Data cleaning & aggregation | `clean_call_log()`, `aggregate_to_call_level()`, `classify_customer_from_activity()` |
| `validate_historical.py` | Validation suite | `validate_arithmetic()`, `validate_historical_consistency()`, `validate_date_ranges()` |
| `historical_log.py` | JSON-based history tracking | `log_week_metrics()`, `verify_historical_consistency()` |
| `store_snapshot.py` | PostgreSQL snapshot + weekly metrics storage | `create_snapshot_table()`, `store_snapshot()`, `create_weekly_metrics_table()`, `store_weekly_metrics()` |
| `ingest_email.py` | MS Graph email ingestion | `get_graph_token()`, `fetch_unread_emails()`, `process_attachments()` |
| `ingest_data.py` | DB ingestion (call_logs + abandoned) | `ingest_call_logs()`, `ingest_abandoned_logs()`, `get_trade_numbers()` |
| `cleanup_data.py` | Archive processed files | `cleanup_data_folder()` |
| `call_analytics_utils.py` | Shared utility functions | Time formatting, phone cleaning, color maps |
| `templates/call_report.html.j2` | Jinja2 HTML report template | Metric cards, charts, tables |

---

## 3. Week Logic Verification - FIXED (2026-01-27)

### 3.1 The Week Swap Problem - FIXED

Previously `generate_plots()` mapped Week 2 (older data) to "This Week" and Week 1 (newest data) to "Last Week". This has been corrected so Week 1 = "This Week" and Week 2 = "Last Week", matching the data assignment in `cleaning.py` and `analyze_calls()`.

### 3.2 The Abandoned Data Cross-Swap - FIXED

Previously, abandoned data was cross-wired: when displaying "This Week" it pulled abandoned calls from the opposite data week. Now each displayed week uses its own abandoned data directly: `week_abd = abd[abd['week'] == week]`.

### 3.3 The Metrics Override - STRUCTURALLY FIXED (2026-06-02)

**History of this bug:** `metrics.update(plot_metrics)` was removed in January 2026, but an AI assistant re-introduced it in February 2026 (commit `ad4b9b3`) because `generate_plots()` still returned a `plot_derived_metrics` dict — making the "merge the second return value" pattern look natural. The bug caused an ~8-call discrepancy in Last Week totals (wrong values propagated to the DB, corrupting future historical comparisons).

**Structural fix (2026-06-02):** All metric-accumulation code has been removed from `generate_plots()`. The function now returns only `{"combined_plot": "<html>..."}` — a single dict, not a tuple. There is no second return value to merge. The call site is `plots = generate_plots(df, abandoned_df)`.

All week-level metrics are computed exclusively in Stage 6 of `analyze_calls()` using midnight-normalised date-range boundaries and are the single source of truth for the report and database.

### 3.4 Historical Consistency

Previous reports logged inconsistent numbers because the override caused the same calendar week to show different totals. Going forward, the same calendar week produces identical numbers whether it appears as "This Week" or "Last Week" in consecutive reports.

### 3.5 Datetime Precision Gap - FIXED (2026-06-02)

`max_date` is now normalised to midnight (`max_date.normalize()`) before computing week boundaries, so all date-range filters use full-day boundaries. Plot bar heights and metric card numbers are derived from the same Stage 6 calculations and will always agree.

---

## 4. Database Storage - FIXED (2026-01-27)

### 4.1 Current State (after fixes)

| Table | File | Persistence | Status |
|---|---|---|---|
| `call_logs` | `call_log_analyzer.py:save_to_database()` | **CREATE IF NOT EXISTS + UPSERT on Call ID** | FIXED - preserves historical data |
| `call_logs` | `ingest_data.py:create_tables()` | DROP + recreate | Still destructive (separate module) |
| `abandoned_calls` | `ingest_data.py` | TRUNCATE each run | Unchanged |
| `report_snapshots` | `store_snapshot.py` | UPSERT on (report_date, week_number) | FIXED - now populates week_start_date/week_end_date |
| `weekly_metrics` | `store_snapshot.py` | **UPSERT on (week_start_date, week_end_date)** | NEW - persistent weekly aggregates |

### 4.2 What Was Fixed (Updated 2026-05-05)

1. **`save_to_database()` no longer destroys data** - Uses `CREATE TABLE IF NOT EXISTS` and upserts on `Call ID`. Historical data is preserved across runs.
   *Note (2026-05-05)*: The script was failing to load DB credentials because it was missing `load_dotenv()`. This has been fixed in `call_log_analyzer.py`.
   *Note (2026-05-05)*: The Postgres `call_logs` table can be missing a unique constraint on `"Call ID"` in older databases. `call_log_analyzer.py` now validates the existing data and adds the constraint automatically before using the `ON CONFLICT` upsert.

2. **`store_snapshot()` now populates week dates** - The `week_start_date` and `week_end_date` columns are now populated from the metrics dict.

3. **New `weekly_metrics` table** - Keyed on `(week_start_date, week_end_date)` so the same calendar week always maps to one row. Uses UPSERT so re-runs update rather than duplicate. Stores: total_calls, retail_calls, trade_calls, abandoned (with retail/trade split), and abandonment rates.

4. **Credentials moved to env vars** - All three files (`call_log_analyzer.py`, `store_snapshot.py`, `ingest_data.py`) now use `os.getenv('DB_PASSWORD')` with no hardcoded fallback. A `.env.example` file documents the required variables.

### 4.3 Database Connection

- Host: `kmc.tequila-ai.com:5432`
- Database: `tequila_ai_reporting`
- User: `james`
- All credentials loaded from `.env` via `python-dotenv`
- See `.env.example` for required variables

---

## 5. Email Ingestion & Automation

### 5.1 Email Reading (ingest_email.py) - EXISTS BUT INCOMPLETE

The email ingestion module is functional:
- Authenticates via MSAL (client credentials flow) to MS Graph API
- Fetches unread emails from `smf_ingestion@tequila-ai.com` sent by `noreply@3cx.net`
- Downloads attachments matching `CallLogLastWeek*.xlsx` and `AbandonedCalls*.xlsx`
- Converts xlsx to csv and saves to `data/` folder
- Marks emails as read

**Missing pieces:**
- Not integrated into `generate_report.py` - must be run manually as a separate step
- No error handling for partial downloads
- No duplicate detection (if run twice, same files downloaded again)
- Environment variables needed: `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` in `.env`

### 5.2 Email Sending Report - DOES NOT EXIST

No code exists to email the generated report to the client. Needs to be built:
- Could use MS Graph API (send endpoint) since auth is already set up
- Or use SMTP
- Should attach `reports/call_report.html` (or inline it)
- Client recipient address needs to be configured

### 5.3 Monday Evening Automation - DOES NOT EXIST

No scheduling mechanism exists. Options:

**Option A - Windows Task Scheduler (simplest for Windows):**
- Create a batch/PowerShell script that runs the full pipeline
- Schedule via Task Scheduler for Monday evenings

**Option B - Cron job (if running on Linux server):**
```
0 18 * * 1 cd /path/to/project && python run_weekly.py
```

**Option C - Cloud-based (most reliable):**
- Azure Functions / AWS Lambda with scheduled trigger
- GitHub Actions with cron schedule

### 5.4 Full Automation Pipeline Needed

A `run_weekly.py` orchestrator script that:
1. Calls `ingest_email.main()` to fetch new data from email
2. Calls `generate_report.generate_report()` to process and generate report
3. Emails the report to the client
4. Calls `cleanup_data.cleanup_data_folder()` to archive processed files
5. Has error handling and logging for each step

---

## 6. Other Issues & Notes

### 6.1 Customer Type Classification

- **Main calls**: Classified by parsing "Call Activity Details" - if text after "Inbound:" starts with a digit, it's retail; otherwise trade. Unknown defaults to retail. (cleaning.py:24-45)
- **Abandoned calls**: Classified by matching Caller ID against known trade phone numbers from the main call log. Non-matches default to retail. (call_log_analyzer.py:800-801)
- Trade numbers are also available from the database `clientlist` table (ingest_data.py:36-50) but this lookup is NOT used in the main analysis pipeline - only in `ingest_data.py` post-processing.

### 6.2 Deduplication

- Main calls: Deduplicated by `Call ID` after concatenating all files (call_log_analyzer.py:736)
- Abandoned calls: Deduplicated by `(Caller ID, Call Time)` pair (call_log_analyzer.py:491)
- This is correct but means if the same call appears in two different weekly export files, it will only be counted once.

### 6.3 Validation System

The validation at report generation time checks:
- Arithmetic: Retail + Trade + Abandoned = Total (per week)
- Total = Week1 + Week2
- No date overlap between weeks
- Historical consistency (compare current "Last Week" vs previous "This Week")

Now that the metrics override has been removed, validation runs on the date-based metrics directly. These should match what the labels claim.

### 6.4 The `call_analytics_utils.py` File

This 29KB utility file contains helper functions but is **not imported by any core module**. It appears to be legacy/reference code. Functions overlap with what's inline in `call_log_analyzer.py`.

### 6.5 Credentials - FIXED

Database credentials have been moved to environment variables in all three files. A `.env.example` file has been created documenting the required variables. The `.env` file itself must be created with the actual password.

### 6.6 Historical Log Duplicates

The JSON log (`reports/historical_weeks.json`) only appends, never checks for duplicates. Running the report twice on the same day produces two entries (visible in the JSON: two entries for 2026-01-06, two for 2026-01-20). Should check if an entry for the same report_date already exists and update instead of append.

---

## 7. Implementation Plan - Remaining Work

### Phase 1: Fix Week Logic - DONE (2026-01-27, structurally hardened 2026-06-02)
- [x] Fixed `get_week_label_display()` - Week 1 = "This Week", Week 2 = "Last Week"
- [x] Fixed loop order to `[1, 2]` and color map to match
- [x] Fixed `metric_week` mapping to direct `f"week{week}"`
- [x] Removed abandoned data cross-swap
- [x] Removed `metrics.update(plot_metrics)` override (Jan 2026)
- [x] Removed metric-accumulation from `generate_plots()` entirely — function now returns chart HTML only, no tuple (Jun 2026). Prevents AI assistants from re-introducing the merge.
- [x] Normalised `max_date` to midnight so date-range boundaries are always full-day aligned (Jun 2026)

### Phase 2: Persistent Database Storage - DONE (2026-01-27)
- [x] Created `weekly_metrics` table with `(week_start_date, week_end_date)` as PK
- [x] Added `store_weekly_metrics()` with UPSERT
- [x] Fixed `store_snapshot()` to populate `week_start_date`/`week_end_date`
- [x] Fixed `save_to_database()` to use `CREATE IF NOT EXISTS` + upsert on Call ID
- [x] Moved hardcoded DB credentials to env vars in all 3 files
- [x] Created `.env.example`

### Phase 3: Email Report to Client (Priority: MEDIUM)
1. Add `send_report_email()` function using MS Graph API send endpoint
2. Configure client recipient address
3. Attach or inline the HTML report
4. Add to the pipeline after report generation

### Phase 4: Monday Evening Automation (Priority: MEDIUM)
1. Create `run_weekly.py` orchestrator script
2. Wire up: email ingest -> generate report -> email report -> cleanup
3. Add comprehensive error handling and logging
4. Set up Windows Task Scheduler (or equivalent) for Monday evening execution

### Phase 5: Data Integrity (Priority: LOW)
1. Fix historical JSON log to prevent duplicate entries
2. Add trade number database lookup to main analysis pipeline
3. Remove unused `call_analytics_utils.py` or integrate needed functions
4. Add data freshness checks (alert if data is stale/missing)

---

## 8. Quick Reference - Key Code Locations

| What | Where |
|---|---|
| Week assignment (per-file) | `cleaning.py:120-139` |
| Week assignment (global override) | `call_log_analyzer.py:~755` |
| Week label display (plot) | `call_log_analyzer.py:~134` (fixed: Week 1="This Week") |
| Customer type classification | `cleaning.py:24-45` |
| Abandoned customer type mapping | `call_log_analyzer.py:~800` |
| Database save (upsert) | `call_log_analyzer.py:21-72` |
| Snapshot storage | `store_snapshot.py:store_snapshot()` |
| Weekly metrics storage | `store_snapshot.py:store_weekly_metrics()` |
| Weekly metrics table schema | `store_snapshot.py:create_weekly_metrics_table()` |
| Email ingestion entry point | `ingest_email.py:151-185` |
| Historical JSON log | `historical_log.py:21-47` |
| Validation checks | `validate_historical.py:11-52` |
| Report template | `templates/call_report.html.j2` |
| DB credentials (env vars) | All files via `dotenv`, see `.env.example` |

---

## 9. Environment & Dependencies

### Python Packages
```
pandas, numpy, plotly, jinja2, psycopg2-binary, msal, requests, openpyxl, python-dotenv, sqlalchemy
```

### Environment Variables Needed
```
DB_HOST=kmc.tequila-ai.com
DB_PORT=5432
DB_NAME=tequila_ai_reporting
DB_USER=james
DB_PASSWORD=<password>
DB_SSLMODE=require

GRAPH_TENANT_ID=<azure tenant id>
GRAPH_CLIENT_ID=<azure app client id>
GRAPH_CLIENT_SECRET=<azure app secret>
IMAP_USER=smf_ingestion@tequila-ai.com
```

### Database Tables
| Table | Status | Notes |
|---|---|---|
| `call_logs` | Exists (upserts on Call ID) | Now persistent across runs |
| `abandoned_calls` | Exists (truncated each run) | Still destructive (ingest_data.py) |
| `report_snapshots` | Exists (persists) | Now populates week_start/end |
| `weekly_metrics` | Exists (persists) | Keyed on (week_start_date, week_end_date) |
| `clientlist` | Exists (external reference) | Used for trade number lookup |

## Coding 
Codex will review your output once you are done.
