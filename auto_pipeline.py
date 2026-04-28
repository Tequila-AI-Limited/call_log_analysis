"""
auto_pipeline.py
================
Automated weekly call log report pipeline.

Schedule: Run every Tuesday at 8:00am GMT via Windows Task Scheduler.

Workflow:
  1. Connect to Microsoft 365 mailbox via Graph API
  2. Find the data email from the configured sender (arrived since Sunday)
  3. Download attachments to data/ folder
  4. Run the report generation script
  5. Email the HTML report to the distribution list
  6. Log all activity to pipeline.log

Configuration (in .env file):
  AZURE_TENANT_ID        - Azure AD Tenant ID
  AZURE_CLIENT_ID        - Azure App Client ID
  AZURE_CLIENT_SECRET    - Azure App Client Secret
  MS_USER_EMAIL          - Your M365 email address (mailbox to read/send from)
  DATA_SOURCE_EMAIL      - Sender address the data files arrive from
  REPORT_RECIPIENTS      - Comma-separated list of report recipient email addresses
"""

import os
import glob
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

import requests
from msal import ConfidentialClientApplication

# ── Import report generator ───────────────────────────────────────────────────
from generate_report import generate_report

# ── Load environment ──────────────────────────────────────────────────────────
load_dotenv()

TENANT_ID       = os.getenv('AZURE_TENANT_ID')
CLIENT_ID       = os.getenv('AZURE_CLIENT_ID')
CLIENT_SECRET   = os.getenv('AZURE_CLIENT_SECRET')
USER_EMAIL      = os.getenv('MS_USER_EMAIL')
DATA_SOURCE     = os.getenv('DATA_SOURCE_EMAIL')
RECIPIENTS_RAW  = os.getenv('REPORT_RECIPIENTS', '')
RECIPIENTS      = [r.strip() for r in RECIPIENTS_RAW.split(',') if r.strip()]

GRAPH_SCOPES    = ['https://graph.microsoft.com/.default']
GRAPH_BASE      = 'https://graph.microsoft.com/v1.0'

DATA_DIR        = Path(__file__).parent / 'data'
REPORTS_DIR     = Path(__file__).parent / 'reports'
LOG_FILE        = Path(__file__).parent / 'pipeline.log'

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Graph API helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_access_token():
    """Obtain a Graph API access token using client credentials flow."""
    app = ConfidentialClientApplication(
        client_id=CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=f'https://login.microsoftonline.com/{TENANT_ID}'
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPES)
    if 'access_token' not in result:
        raise RuntimeError(f"Failed to obtain access token: {result.get('error_description')}")
    log.info("Access token obtained successfully.")
    return result['access_token']


def graph_get(token, url, params=None):
    """Perform a GET request against the Graph API."""
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def graph_post(token, url, payload):
    """Perform a POST request against the Graph API."""
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Fetch the data email and download attachments
# ─────────────────────────────────────────────────────────────────────────────

def fetch_data_attachments(token):
    """
    Find the latest unread email from DATA_SOURCE_EMAIL received since last Sunday,
    download its attachments to data/, mark the email as read.
    Returns True if attachments were downloaded, False otherwise.
    """
    # Search window: emails received since last Sunday midnight UTC
    since_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime('%Y-%m-%dT00:00:00Z')
    
    filter_query = (
        f"from/emailAddress/address eq '{DATA_SOURCE}' "
        f"and isRead eq false "
        f"and receivedDateTime ge {since_date}"
    )
    
    url = f"{GRAPH_BASE}/users/{USER_EMAIL}/messages"
    params = {
        '$filter': filter_query,
        '$orderby': 'receivedDateTime desc',
        '$top': '5',
        '$select': 'id,subject,receivedDateTime,hasAttachments'
    }
    
    log.info(f"Searching for emails from: {DATA_SOURCE}")
    data = graph_get(token, url, params=params)
    messages = data.get('value', [])
    
    if not messages:
        log.warning(f"No unread data emails found from {DATA_SOURCE} since {since_date}.")
        return False
    
    # Take the most recent matching email
    message = messages[0]
    message_id = message['id']
    subject = message.get('subject', '(no subject)')
    received = message.get('receivedDateTime', '')
    log.info(f"Found email: '{subject}' received {received}")
    
    if not message.get('hasAttachments'):
        log.error(f"Email found but has no attachments: '{subject}'")
        return False
    
    # Fetch attachments
    attachments_url = f"{GRAPH_BASE}/users/{USER_EMAIL}/messages/{message_id}/attachments"
    att_data = graph_get(token, attachments_url)
    attachments = att_data.get('value', [])
    
    if not attachments:
        log.error("No attachments returned for this message.")
        return False
    
    DATA_DIR.mkdir(exist_ok=True)
    downloaded = 0
    
    for att in attachments:
        name = att.get('name', 'attachment')
        content_bytes = att.get('contentBytes')  # base64 encoded
        
        if not content_bytes:
            log.warning(f"Attachment '{name}' has no content, skipping.")
            continue
        
        import base64
        file_data = base64.b64decode(content_bytes)
        dest_path = DATA_DIR / name
        
        with open(dest_path, 'wb') as f:
            f.write(file_data)
        
        log.info(f"Downloaded attachment: {name} ({len(file_data):,} bytes) → data/{name}")
        downloaded += 1
    
    if downloaded == 0:
        log.error("No attachments could be downloaded.")
        return False
    
    # Mark email as read
    patch_url = f"{GRAPH_BASE}/users/{USER_EMAIL}/messages/{message_id}"
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    requests.patch(patch_url, headers=headers, json={'isRead': True}, timeout=30)
    log.info(f"Marked email as read: '{subject}'")
    
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Run the report
# ─────────────────────────────────────────────────────────────────────────────

def run_report():
    """
    Call the report generator. Returns the path to the generated HTML report,
    or None if generation failed.
    """
    log.info("Starting report generation...")
    
    # Change working directory so generate_report.py finds its relative paths
    original_dir = os.getcwd()
    os.chdir(Path(__file__).parent)
    
    try:
        generate_report()
    except Exception as e:
        log.error(f"Report generation raised an exception: {e}")
        return None
    finally:
        os.chdir(original_dir)
    
    # Find the most recently generated report
    report_files = sorted(REPORTS_DIR.glob('call_report_*.html'), key=os.path.getmtime, reverse=True)
    
    if not report_files:
        log.error("Report generation completed but no HTML file was found.")
        return None
    
    report_path = report_files[0]
    log.info(f"Report ready: {report_path.name}")
    return report_path


# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Send the report email
# ─────────────────────────────────────────────────────────────────────────────

def send_report_email(token, report_path, metrics_summary=None):
    """
    Email the HTML report to the distribution list via Graph API.
    Attaches the HTML report and includes a brief summary in the body.
    """
    import base64
    
    report_name = report_path.name
    with open(report_path, 'rb') as f:
        report_b64 = base64.b64encode(f.read()).decode('utf-8')
    
    today_str = datetime.now().strftime('%d %B %Y')
    body_text = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333;">
    <p>Hi all,</p>
    <p>Please find the weekly call performance report for the period ending {today_str} attached.</p>
    {f'<p>{metrics_summary}</p>' if metrics_summary else ''}
    <p>Please reach out if you have any questions.</p>
    <p>Kind regards</p>
    </body></html>
    """
    
    to_recipients = [{'emailAddress': {'address': r}} for r in RECIPIENTS]
    
    payload = {
        'message': {
            'subject': f'Weekly Call Performance Report – {today_str}',
            'body': {
                'contentType': 'HTML',
                'content': body_text
            },
            'toRecipients': to_recipients,
            'attachments': [
                {
                    '@odata.type': '#microsoft.graph.fileAttachment',
                    'name': report_name,
                    'contentType': 'text/html',
                    'contentBytes': report_b64
                }
            ]
        },
        'saveToSentItems': True
    }
    
    send_url = f"{GRAPH_BASE}/users/{USER_EMAIL}/sendMail"
    graph_post(token, send_url, payload)
    log.info(f"Report emailed to {len(RECIPIENTS)} recipient(s): {', '.join(RECIPIENTS)}")


def send_alert_email(token, subject, body_text):
    """Send an alert/error email back to the sender (you) if something goes wrong."""
    payload = {
        'message': {
            'subject': f'[ALERT] Call Log Pipeline: {subject}',
            'body': {
                'contentType': 'Text',
                'content': body_text
            },
            'toRecipients': [{'emailAddress': {'address': USER_EMAIL}}]
        },
        'saveToSentItems': False
    }
    try:
        send_url = f"{GRAPH_BASE}/users/{USER_EMAIL}/sendMail"
        graph_post(token, send_url, payload)
        log.info(f"Alert email sent to {USER_EMAIL}: {subject}")
    except Exception as e:
        log.error(f"Failed to send alert email: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline():
    log.info("=" * 60)
    log.info("CALL LOG REPORT PIPELINE — STARTING")
    log.info(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)
    
    # Validate config
    missing = [k for k, v in {
        'AZURE_TENANT_ID': TENANT_ID,
        'AZURE_CLIENT_ID': CLIENT_ID,
        'AZURE_CLIENT_SECRET': CLIENT_SECRET,
        'MS_USER_EMAIL': USER_EMAIL,
        'DATA_SOURCE_EMAIL': DATA_SOURCE,
        'REPORT_RECIPIENTS': RECIPIENTS
    }.items() if not v]
    
    if missing:
        log.error(f"Missing required environment variables: {', '.join(missing)}")
        log.error("Please update your .env file and try again.")
        return
    
    # Get Graph API token
    try:
        token = get_access_token()
    except Exception as e:
        log.error(f"Authentication failed: {e}")
        return
    
    # Step 1: Fetch data attachments
    try:
        fetched = fetch_data_attachments(token)
    except Exception as e:
        log.error(f"Error fetching data email: {e}")
        send_alert_email(token, "Data email fetch failed", str(e))
        return
    
    if not fetched:
        msg = (
            f"No data email was found from '{DATA_SOURCE}'.\n"
            "The pipeline did not run. Please check your inbox and re-run manually if needed."
        )
        log.warning(msg)
        send_alert_email(token, "No data email found", msg)
        return
    
    # Step 2: Generate the report
    try:
        report_path = run_report()
    except Exception as e:
        log.error(f"Unexpected error during report generation: {e}")
        send_alert_email(token, "Report generation failed", str(e))
        return
    
    if not report_path:
        msg = "Report generation failed. Check pipeline.log for details."
        log.error(msg)
        send_alert_email(token, "Report generation failed", msg)
        return
    
    # Step 3: Email the report
    try:
        send_report_email(token, report_path)
    except Exception as e:
        log.error(f"Failed to send report email: {e}")
        send_alert_email(token, "Report email delivery failed", str(e))
        return
    
    log.info("=" * 60)
    log.info("PIPELINE COMPLETED SUCCESSFULLY")
    log.info("=" * 60)


if __name__ == '__main__':
    run_pipeline()
