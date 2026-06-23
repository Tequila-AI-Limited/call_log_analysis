"""Automated weekly call-log report pipeline.

Orchestrates the full end-to-end workflow that runs every Tuesday morning:

1. Authenticate with Microsoft 365 via Azure AD (client credentials).
2. Find the latest unread email from the 3CX data sender.
3. Download its attachments (call-log and abandoned-call files) to ``data/``.
4. Generate the standard report via ``generate_report`` (writes to DB, exports CSVs).
5. Generate the enhanced stakeholder report with AI executive summary.
6. Email the enhanced report to the distribution list.
7. Log all activity to ``pipeline.log``.

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

    python run_weekly.py
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
from generate_enhanced_report import generate_enhanced_report


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

TENANT_ID = os.getenv("AZURE_TENANT_ID")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
USER_EMAIL = os.getenv("MS_USER_EMAIL")
DATA_MAILBOX = os.getenv("DATA_MAILBOX", USER_EMAIL)
SEND_MAILBOX = os.getenv("SEND_MAILBOX", USER_EMAIL)
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
    if not response.ok:
        log.error("Graph GET failed: %s", response.text)
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
    """Download call-log attachments from recent data emails.

    The 3CX reports arrive as several separate emails, so this processes all
    matching messages in the recent window rather than only the newest one.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%Y-%m-%dT00:00:00Z"
    )

    def search_messages(unread_only: bool) -> list[dict]:
        filter_parts = [
            f"receivedDateTime ge {since}",
            f"from/emailAddress/address eq '{DATA_SOURCE}'",
        ]
        if unread_only:
            filter_parts.append("isRead eq false")

        params = {
            "$filter": " and ".join(filter_parts),
            "$orderby": "receivedDateTime desc",
            "$top": "10",
            "$select": "id,subject,receivedDateTime,hasAttachments,isRead",
        }
        return graph_get(
            token, f"{GRAPH_BASE}/users/{DATA_MAILBOX}/messages", params=params
        ).get("value", [])

    log.info(f"Searching for unread data emails from: {DATA_SOURCE}")
    messages = search_messages(unread_only=True)

    if not messages:
        log.warning(
            f"No unread data emails found from {DATA_SOURCE} since {since}; "
            "checking recent read emails as a fallback."
        )
        messages = search_messages(unread_only=False)

    if not messages:
        log.warning(f"No data emails found from {DATA_SOURCE} since {since}.")
        return False

    DATA_DIR.mkdir(exist_ok=True)
    downloaded = 0

    for message in messages:
        message_id = message["id"]
        subject = message.get("subject", "(no subject)")
        log.info(
            f"Found email: '{subject}' "
            f"(received {message.get('receivedDateTime', '')})"
        )

        if not message.get("hasAttachments"):
            log.warning(f"Email has no attachments: '{subject}'")
            continue

        attachments = graph_get(
            token,
            f"{GRAPH_BASE}/users/{DATA_MAILBOX}/messages/{message_id}/attachments",
        ).get("value", [])

        if not attachments:
            log.warning(f"No attachments returned for email: '{subject}'")
            continue

        message_downloaded = 0
        for att in attachments:
            name = att.get("name", "attachment")
            content_bytes = att.get("contentBytes")
            if not content_bytes:
                log.warning(f"Attachment '{name}' has no content - skipping.")
                continue

            dest = DATA_DIR / name
            dest.write_bytes(base64.b64decode(content_bytes))
            log.info(
                f"Downloaded: {name} ({dest.stat().st_size:,} bytes) -> data/{name}"
            )
            downloaded += 1
            message_downloaded += 1

        if message_downloaded and not message.get("isRead"):
            response = requests.patch(
                f"{GRAPH_BASE}/users/{DATA_MAILBOX}/messages/{message_id}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"isRead": True},
                timeout=30,
            )
            if response.ok:
                log.info(f"Marked email as read: '{subject}'")
            else:
                log.warning(f"Could not mark email as read: {response.text}")

    if downloaded == 0:
        log.error("No attachments could be downloaded.")
        return False

    log.info(f"Downloaded {downloaded} attachment(s) from {len(messages)} email(s).")
    return True


def run_report() -> Path | None:
    """Generate the standard report (DB writes + CSV exports) then the enhanced
    stakeholder report with AI executive summary.

    Returns:
        Path to the enhanced stakeholder HTML file to send, or ``None`` if
        either generation step failed.
    """
    log.info("Starting standard report generation (DB + CSV exports)...")
    original_dir = os.getcwd()
    os.chdir(Path(__file__).parent)
    try:
        generate_report()
    except Exception as exc:
        log.error(f"Standard report generation raised an exception: {exc}")
        return None
    finally:
        os.chdir(original_dir)

    log.info("Generating enhanced stakeholder report with AI executive summary...")
    try:
        enhanced_path = generate_enhanced_report()
    except Exception as exc:
        log.error(f"Enhanced report generation raised an exception: {exc}")
        return None

    log.info(f"Enhanced report ready: {enhanced_path.name}")
    return enhanced_path


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
        "<p>Hi,</p>"
        f"<p>Please find the weekly call performance report for the period ending "
        f"{today} attached.</p>"
        f"{f'<p>{metrics_summary}</p>' if metrics_summary else ''}"
        "<p>Please reach out if you have any questions.</p>"
        "<br><br>"
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
    graph_post(token, f"{GRAPH_BASE}/users/{SEND_MAILBOX}/sendMail", payload)
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
            "toRecipients": [{"emailAddress": {"address": SEND_MAILBOX}}],
        },
        "saveToSentItems": False,
    }
    try:
        graph_post(token, f"{GRAPH_BASE}/users/{SEND_MAILBOX}/sendMail", payload)
        log.info(f"Alert sent to {SEND_MAILBOX}: {subject}")
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
        "DATA_MAILBOX": DATA_MAILBOX,
        "SEND_MAILBOX": SEND_MAILBOX,
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
