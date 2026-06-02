"""Automated weekly call-log report pipeline.

Orchestrates the full end-to-end workflow that runs every Tuesday morning:

1. Authenticate with Microsoft 365 via Azure AD (client credentials).
2. Find the latest unread email from the 3CX data sender.
3. Download its attachments (call-log and abandoned-call files) to ``data/``.
4. Generate the weekly HTML report via ``generate_report.generate_report()``.
5. Email the report to the distribution list.
6. Log all activity to ``pipeline.log``.

Required environment variables (set in ``.env``):

.. code-block:: ini

    AZURE_TENANT_ID      – Azure AD tenant ID
    AZURE_CLIENT_ID      – Azure App client ID
    AZURE_CLIENT_SECRET  – Azure App client secret
    MS_USER_EMAIL        – M365 mailbox used to read and send mail
    DATA_SOURCE_EMAIL    – Sender address of the 3CX data email
    REPORT_RECIPIENTS    – Comma-separated recipient addresses

Schedule this script via Windows Task Scheduler or equivalent to run every
Tuesday at 08:00 GMT::

    python auto_pipeline.py
"""

import base64
import logging
import os
import glob
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from msal import ConfidentialClientApplication

from generate_report import generate_report


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

TENANT_ID = os.getenv("AZURE_TENANT_ID")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
USER_EMAIL = os.getenv("MS_USER_EMAIL")
DATA_SOURCE = os.getenv("DATA_SOURCE_EMAIL")
RECIPIENTS = [r.strip() for r in os.getenv("REPORT_RECIPIENTS", "").split(",") if r.strip()]

GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

DATA_DIR = Path(__file__).parent / "data"
REPORTS_DIR = Path(__file__).parent / "reports"
LOG_FILE = Path(__file__).parent / "pipeline.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Graph API helpers
# ---------------------------------------------------------------------------


def get_access_token() -> str:
    """Obtain a Microsoft Graph API access token using client credentials.

    Uses MSAL's ``ConfidentialClientApplication`` to acquire a token for the
    ``https://graph.microsoft.com/.default`` scope.

    Returns:
        A valid Bearer token string.

    Raises:
        RuntimeError: If MSAL cannot acquire a token (invalid credentials,
            network error, etc.).
    """
    app = ConfidentialClientApplication(
        client_id=CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(
            f"Failed to acquire Graph API token: {result.get('error_description')}"
        )
    log.info("Graph API access token acquired.")
    return result["access_token"]


def graph_get(token: str, url: str, params: dict | None = None) -> dict:
    """Perform an authenticated GET request against the Microsoft Graph API.

    Args:
        token: A valid Bearer access token.
        url: Full Graph API endpoint URL.
        params: Optional query parameters (passed as ``$filter``, ``$top``,
            etc.).

    Returns:
        Parsed JSON response as a dict.

    Raises:
        requests.HTTPError: If the server returns a non-2xx status code.
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def graph_post(token: str, url: str, payload: dict) -> requests.Response:
    """Perform an authenticated POST request against the Microsoft Graph API.

    Args:
        token: A valid Bearer access token.
        url: Full Graph API endpoint URL.
        payload: JSON-serialisable request body.

    Returns:
        The raw ``requests.Response`` object (caller can inspect status).

    Raises:
        requests.HTTPError: If the server returns a non-2xx status code.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    return response


# ---------------------------------------------------------------------------
# Step 1: Fetch data attachments
# ---------------------------------------------------------------------------


def fetch_data_attachments(token: str) -> bool:
    """Download call-log attachments from the latest unread data email.

    Searches the mailbox for an unread email from ``DATA_SOURCE_EMAIL``
    received within the past two days, downloads all its attachments to
    ``data/``, and marks the email as read.

    Args:
        token: A valid Graph API Bearer token.

    Returns:
        ``True`` if at least one attachment was downloaded, ``False``
        otherwise.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%Y-%m-%dT00:00:00Z"
    )
    params = {
        "$filter": (
            f"from/emailAddress/address eq '{DATA_SOURCE}' "
            f"and isRead eq false "
            f"and receivedDateTime ge {since}"
        ),
        "$orderby": "receivedDateTime desc",
        "$top": "5",
        "$select": "id,subject,receivedDateTime,hasAttachments",
    }

    log.info(f"Searching for data email from: {DATA_SOURCE}")
    messages = graph_get(
        token, f"{GRAPH_BASE}/users/{USER_EMAIL}/messages", params=params
    ).get("value", [])

    if not messages:
        log.warning(f"No unread data emails found from {DATA_SOURCE} since {since}.")
        return False

    message = messages[0]
    message_id = message["id"]
    subject = message.get("subject", "(no subject)")
    log.info(f"Found email: '{subject}' (received {message.get('receivedDateTime', '')})")

    if not message.get("hasAttachments"):
        log.error(f"Email has no attachments: '{subject}'")
        return False

    attachments = graph_get(
        token,
        f"{GRAPH_BASE}/users/{USER_EMAIL}/messages/{message_id}/attachments",
    ).get("value", [])

    if not attachments:
        log.error("No attachments returned for this message.")
        return False

    DATA_DIR.mkdir(exist_ok=True)
    downloaded = 0
    for att in attachments:
        name = att.get("name", "attachment")
        content_bytes = att.get("contentBytes")
        if not content_bytes:
            log.warning(f"Attachment '{name}' has no content — skipping.")
            continue
        dest = DATA_DIR / name
        dest.write_bytes(base64.b64decode(content_bytes))
        log.info(f"Downloaded: {name} ({dest.stat().st_size:,} bytes) → data/{name}")
        downloaded += 1

    if downloaded == 0:
        log.error("No attachments could be downloaded.")
        return False

    # Mark as read.
    requests.patch(
        f"{GRAPH_BASE}/users/{USER_EMAIL}/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"isRead": True},
        timeout=30,
    )
    log.info(f"Marked email as read: '{subject}'")
    return True


# ---------------------------------------------------------------------------
# Step 2: Run the report
# ---------------------------------------------------------------------------


def run_report() -> Path | None:
    """Invoke ``generate_report`` and return the path to the output HTML file.

    Changes the working directory to the project root before calling the
    report generator so that all its relative paths resolve correctly.

    Returns:
        Path to the most recently generated ``call_report_*.html`` file, or
        ``None`` if generation failed.
    """
    log.info("Starting report generation...")
    original_dir = os.getcwd()
    os.chdir(Path(__file__).parent)
    try:
        generate_report()
    except Exception as exc:
        log.error(f"Report generation raised an exception: {exc}")
        return None
    finally:
        os.chdir(original_dir)

    reports = sorted(
        REPORTS_DIR.glob("call_report_*.html"), key=os.path.getmtime, reverse=True
    )
    if not reports:
        log.error("Report generation completed but no HTML file was found.")
        return None

    log.info(f"Report ready: {reports[0].name}")
    return reports[0]


# ---------------------------------------------------------------------------
# Step 3: Send the report email
# ---------------------------------------------------------------------------


def send_report_email(
    token: str, report_path: Path, metrics_summary: str | None = None
) -> None:
    """Email the HTML report to the distribution list via Graph API.

    Attaches the HTML file and includes a brief plain-text summary in the
    email body.

    Args:
        token: A valid Graph API Bearer token.
        report_path: Path to the ``call_report_*.html`` file to attach.
        metrics_summary: Optional HTML snippet to embed in the email body.
    """
    report_b64 = base64.b64encode(report_path.read_bytes()).decode("utf-8")
    today = datetime.now().strftime("%d %B %Y")

    body_html = (
        "<html><body style=\"font-family:Arial,sans-serif;color:#333;\">"
        "<p>Hi all,</p>"
        f"<p>Please find the weekly call performance report for the period ending "
        f"{today} attached.</p>"
        f"{f'<p>{metrics_summary}</p>' if metrics_summary else ''}"
        "<p>Please reach out if you have any questions.</p>"
        "<p>Kind regards</p>"
        "</body></html>"
    )

    payload = {
        "message": {
            "subject": f"Weekly Call Performance Report – {today}",
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": r}} for r in RECIPIENTS],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": report_path.name,
                    "contentType": "text/html",
                    "contentBytes": report_b64,
                }
            ],
        },
        "saveToSentItems": True,
    }
    graph_post(token, f"{GRAPH_BASE}/users/{USER_EMAIL}/sendMail", payload)
    log.info(f"Report emailed to {len(RECIPIENTS)} recipient(s): {', '.join(RECIPIENTS)}")


def send_alert_email(token: str, subject: str, body_text: str) -> None:
    """Send a plain-text alert email back to the pipeline operator.

    Used to notify the operator when any pipeline stage fails so they can
    intervene manually.

    Args:
        token: A valid Graph API Bearer token.
        subject: Short description of the failure (appended to ``[ALERT]``).
        body_text: Full error detail to include in the email body.
    """
    payload = {
        "message": {
            "subject": f"[ALERT] Call Log Pipeline: {subject}",
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": USER_EMAIL}}],
        },
        "saveToSentItems": False,
    }
    try:
        graph_post(token, f"{GRAPH_BASE}/users/{USER_EMAIL}/sendMail", payload)
        log.info(f"Alert sent to {USER_EMAIL}: {subject}")
    except Exception as exc:
        log.error(f"Failed to send alert email: {exc}")


# ---------------------------------------------------------------------------
# Main pipeline orchestrator
# ---------------------------------------------------------------------------


def run_pipeline() -> None:
    """Execute the full automated pipeline end-to-end.

    Validates configuration, acquires a Graph API token, fetches data
    attachments, generates the report, and emails it to recipients.  At each
    stage, failures are logged and an alert email is sent before the function
    returns early.
    """
    log.info("=" * 60)
    log.info("CALL LOG REPORT PIPELINE — STARTING")
    log.info(f"Run time: {datetime.now():%Y-%m-%d %H:%M:%S}")
    log.info("=" * 60)

    # Validate required environment variables.
    required = {
        "AZURE_TENANT_ID": TENANT_ID,
        "AZURE_CLIENT_ID": CLIENT_ID,
        "AZURE_CLIENT_SECRET": CLIENT_SECRET,
        "MS_USER_EMAIL": USER_EMAIL,
        "DATA_SOURCE_EMAIL": DATA_SOURCE,
        "REPORT_RECIPIENTS": RECIPIENTS,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        log.error(f"Missing environment variables: {', '.join(missing)}")
        return

    try:
        token = get_access_token()
    except Exception as exc:
        log.error(f"Authentication failed: {exc}")
        return

    # Step 1: Fetch attachments.
    try:
        fetched = fetch_data_attachments(token)
    except Exception as exc:
        log.error(f"Error fetching data email: {exc}")
        send_alert_email(token, "Data email fetch failed", str(exc))
        return

    if not fetched:
        msg = (
            f"No data email found from '{DATA_SOURCE}'.\n"
            "The pipeline did not run.  Please check the inbox and re-run manually."
        )
        log.warning(msg)
        send_alert_email(token, "No data email found", msg)
        return

    # Step 2: Generate report.
    try:
        report_path = run_report()
    except Exception as exc:
        log.error(f"Unexpected error during report generation: {exc}")
        send_alert_email(token, "Report generation failed", str(exc))
        return

    if not report_path:
        msg = "Report generation failed.  Check pipeline.log for details."
        log.error(msg)
        send_alert_email(token, "Report generation failed", msg)
        return

    # Step 3: Email the report.
    try:
        send_report_email(token, report_path)
    except Exception as exc:
        log.error(f"Failed to send report email: {exc}")
        send_alert_email(token, "Report email delivery failed", str(exc))
        return

    log.info("=" * 60)
    log.info("PIPELINE COMPLETED SUCCESSFULLY")
    log.info("=" * 60)


if __name__ == "__main__":
    run_pipeline()
