"""
Historical Report Snapshot Storage
Stores metrics from each report run to enable historical comparisons.
"""
import psycopg2
from datetime import datetime, date
import pandas as pd
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Database configuration (loaded from .env)
DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'port': os.getenv('DB_PORT'),
    'database': os.getenv('DB_NAME'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'sslmode': os.getenv('DB_SSLMODE', 'require')
}

def get_db_connection():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        return None

def create_snapshot_table():
    """Create report_snapshots table if it doesn't exist."""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS report_snapshots (
                id SERIAL PRIMARY KEY,
                report_date DATE NOT NULL,
                week_number INTEGER NOT NULL,
                week_label TEXT,
                week_start_date DATE,
                week_end_date DATE,
                total_calls INTEGER,
                retail_calls INTEGER,
                trade_calls INTEGER,
                abandoned_calls INTEGER,
                answered_calls INTEGER,
                abandonment_rate DECIMAL(5,2),
                retail_abandonment_rate DECIMAL(5,2),
                trade_abandonment_rate DECIMAL(5,2),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(report_date, week_number)
            );
        """)
        conn.commit()
        cursor.close()
        print("Report snapshots table ready.")
        return True
    except Exception as e:
        print(f"Error creating table: {e}")
        return False
    finally:
        conn.close()

def store_snapshot(metrics, report_date=None):
    """Store metrics snapshot for historical comparison."""
    if report_date is None:
        report_date = date.today()
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        
        # Store Week 1 (This Week)
        cursor.execute("""
            INSERT INTO report_snapshots (
                report_date, week_number, week_label,
                total_calls, retail_calls, trade_calls,
                abandoned_calls, abandonment_rate,
                retail_abandonment_rate, trade_abandonment_rate
            ) VALUES (%s, 1, 'This Week', %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (report_date, week_number) DO UPDATE SET
                total_calls = EXCLUDED.total_calls,
                retail_calls = EXCLUDED.retail_calls,
                trade_calls = EXCLUDED.trade_calls,
                abandoned_calls = EXCLUDED.abandoned_calls,
                abandonment_rate = EXCLUDED.abandonment_rate,
                retail_abandonment_rate = EXCLUDED.retail_abandonment_rate,
                trade_abandonment_rate = EXCLUDED.trade_abandonment_rate
        """, (
            report_date,
            metrics['week1_calls'],
            metrics['week1_retail_total'],
            metrics['week1_trade_total'],
            metrics['week1_retail_abandoned'] + metrics['week1_trade_abandoned'],
            metrics['abandonment_rate'],
            metrics['week1_retail_abandonment_rate'],
            metrics['week1_trade_abandonment_rate']
        ))
        
        # Store Week 2 (Last Week)
        cursor.execute("""
            INSERT INTO report_snapshots (
                report_date, week_number, week_label,
                total_calls, retail_calls, trade_calls,
                abandoned_calls, abandonment_rate,
                retail_abandonment_rate, trade_abandonment_rate
            ) VALUES (%s, 2, 'Last Week', %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (report_date, week_number) DO UPDATE SET
                total_calls = EXCLUDED.total_calls,
                retail_calls = EXCLUDED.retail_calls,
                trade_calls = EXCLUDED.trade_calls,
                abandoned_calls = EXCLUDED.abandoned_calls,
                abandonment_rate = EXCLUDED.abandonment_rate,
                retail_abandonment_rate = EXCLUDED.retail_abandonment_rate,
                trade_abandonment_rate = EXCLUDED.trade_abandonment_rate
        """, (
            report_date,
            metrics['week2_calls'],
            metrics['week2_retail_total'],
            metrics['week2_trade_total'],
            metrics['week2_retail_abandoned'] + metrics['week2_trade_abandoned'],
            metrics['abandonment_rate'],
            metrics['week2_retail_abandonment_rate'],
            metrics['week2_trade_abandonment_rate']
        ))
        
        conn.commit()
        cursor.close()
        print(f"Stored snapshot for {report_date}")
        return True
        
    except Exception as e:
        print(f"Error storing snapshot: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        conn.close()

def get_previous_report_comparison():
    """Get the previous report's 'This Week' to compare with current 'Last Week'."""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        query = """
            SELECT report_date, total_calls, retail_calls, trade_calls, 
                   abandoned_calls, abandonment_rate
            FROM report_snapshots
            WHERE week_label = 'This Week'
            ORDER BY report_date DESC
            LIMIT 1 OFFSET 1
        """
        df = pd.read_sql(query, conn)
        return df if not df.empty else None
    except Exception as e:
        print(f"Error fetching previous report: {e}")
        return None
    finally:
        conn.close()

if __name__ == "__main__":
    # Test table creation
    create_snapshot_table()
    print("Snapshot table created successfully!")
