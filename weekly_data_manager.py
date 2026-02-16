import csv
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pandas as pd
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'port': os.getenv('DB_PORT'),
    'database': os.getenv('DB_NAME'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'sslmode': os.getenv('DB_SSLMODE')
}

TABLE_NAME = 'weekly_stats'

# Define CSV columns (kept for reference or fallback if needed)
COLUMNS = [
    'week_start', 'week_end', 
    'total_calls', 
    'retail_calls', 'trade_calls', 
    'abandoned_total', 
    'retail_abandoned', 'trade_abandoned',
    'report_generated_date'
]

def get_db_connection():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        return None

def initialize_db():
    """Initialize the Database table if it doesn't exist."""
    conn = get_db_connection()
    if not conn:
        return

    try:
        cursor = conn.cursor()
        create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            id SERIAL PRIMARY KEY,
            week_start VARCHAR(20),
            week_end VARCHAR(20),
            total_calls INTEGER,
            retail_calls INTEGER,
            trade_calls INTEGER,
            abandoned_total INTEGER,
            retail_abandoned INTEGER,
            trade_abandoned INTEGER,
            report_generated_date TIMESTAMP,
            UNIQUE(week_start, week_end)
        );
        """
        cursor.execute(create_table_sql)
        conn.commit()
        # print(f"Initialized weekly data table '{TABLE_NAME}' in database.")
    except Exception as e:
        print(f"Error initializing database table: {e}")
    finally:
        cursor.close()
        conn.close()

def load_week_data(start_date, end_date):
    """
    Load data for a specific week range from Database.
    Returns a dictionary of metrics if found, else None.
    Dates should be strings in 'DD/MM/YYYY' format or datetime objects.
    """
    # Normalize dates to string format 'YYYY-MM-DD' for comparison (PostgreSQL/ISO)
    if isinstance(start_date, datetime):
        start_date = start_date.strftime('%Y-%m-%d')
    elif isinstance(start_date, str) and '/' in start_date:
        # Attempt minimal conversion from DD/MM/YYYY to YYYY-MM-DD if needed
        try:
            start_date = datetime.strptime(start_date, '%d/%m/%Y').strftime('%Y-%m-%d')
        except ValueError:
            pass # Keep original string if parsing fails

    if isinstance(end_date, datetime):
        end_date = end_date.strftime('%Y-%m-%d')
    elif isinstance(end_date, str) and '/' in end_date:
        try:
            end_date = datetime.strptime(end_date, '%d/%m/%Y').strftime('%Y-%m-%d')
        except ValueError:
            pass

    conn = get_db_connection()
    if not conn:
        return None

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # We store normalized dates in DB as strings for consistency with legacy CSV format
        # or we could use Date type. Current implementation assumes string match.
        # But wait, ingest script uses whatever is in CSV. CSV has YYYY-MM-DD usually?
        # Let's check weekly_data.csv content from Step 13: 
        # 2026-02-02,2026-02-08 ...
        # The code in generate_report Step 29 fmt_date expects YYYY-MM-DD.
        # But weekly_data_manager Step 30 load_week_data converted input to DD/MM/YYYY?
        # Step 30: 
        #   if isinstance(start_date, datetime): start_date = start_date.strftime('%d/%m/%Y')
        #   match = df[(df['week_start'] == start_date) ...]
        #
        # If the CSV has YYYY-MM-DD, and we search for DD/MM/YYYY, that would fail unless the CSV also had DD/MM/YYYY.
        # Let's check Step 13 again.
        # Step 13: 2026-02-02,2026-02-08.
        # So the old code might have been buggy or I misread how it was being used.
        # generate_report passes strings from `analyze_calls`.
        # `analyze_calls` likely returns YYYY-MM-DD strings.
        
        # To be safe, I should allow for flexible date matching or stick to what is in the DB.
        # The ingest script dumps what is in the CSV.
        # If the CSV has YYYY-MM-DD, the DB will have YYYY-MM-DD.
        # I should assume YYYY-MM-DD is the standard.
        
        # Let's try to match exactly first.
        
        query = f"SELECT * FROM {TABLE_NAME} WHERE week_start = %s AND week_end = %s"
        cursor.execute(query, (start_date, end_date))
        row = cursor.fetchone()
        
        if row:
            return dict(row)
        
        # Fallback: try converting MM/DD/YYYY or DD/MM/YYYY if needed?
        # For now, simplistic.
        
        return None

    except Exception as e:
        print(f"Error loading week data from DB: {e}")
        return None
    finally:
        if conn:
            cursor.close()
            conn.close()

def save_week_data(metrics):
    """
    Save or update metrics for a week in Database.
    metrics dict must contain:
    - start_date, end_date
    - total, retail, trade
    - abandoned, abandoned_retail, abandoned_trade
    """
    initialize_db()
    
    start_date = metrics.get('start_date')
    end_date = metrics.get('end_date')
    
    conn = get_db_connection()
    if not conn:
        print("Failed to connect to DB for saving.")
        return

    try:
        cursor = conn.cursor()
        
        upsert_sql = f"""
        INSERT INTO {TABLE_NAME} 
        (week_start, week_end, total_calls, retail_calls, trade_calls, abandoned_total, retail_abandoned, trade_abandoned, report_generated_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (week_start, week_end) DO UPDATE SET
            total_calls = EXCLUDED.total_calls,
            retail_calls = EXCLUDED.retail_calls,
            trade_calls = EXCLUDED.trade_calls,
            abandoned_total = EXCLUDED.abandoned_total,
            retail_abandoned = EXCLUDED.retail_abandoned,
            trade_abandoned = EXCLUDED.trade_abandoned,
            report_generated_date = EXCLUDED.report_generated_date;
        """
        
        values = (
            start_date,
            end_date,
            metrics.get('total', 0),
            metrics.get('retail', 0),
            metrics.get('trade', 0),
            metrics.get('abandoned', 0),
            metrics.get('abandoned_retail', 0),
            metrics.get('abandoned_trade', 0),
            datetime.now()
        )
        
        cursor.execute(upsert_sql, values)
        conn.commit()
        print(f"Saved weekly data for {start_date} - {end_date} to Database.")

    except Exception as e:
        print(f"Error saving to DB: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()

def get_all_weeks():
    """Return all stored weeks."""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        # Use pandas for easy dict conversion
        query = f"SELECT * FROM {TABLE_NAME}"
        df = pd.read_sql(query, conn)
        return df.to_dict('records')
    except Exception as e:
        print(f"Error getting all weeks: {e}")
        return []
    finally:
        conn.close()
