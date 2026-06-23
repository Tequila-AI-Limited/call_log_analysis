"""Print recent Microsoft Graph mailbox message metadata for pipeline setup.

This is a non-destructive helper for confirming which mailbox and sender address
Graph can see. It does not download attachments or mark messages read.
"""

import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from msal import ConfidentialClientApplication


load_dotenv()

TENANT_ID = os.getenv("AZURE_TENANT_ID")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
DATA_MAILBOX = os.getenv("DATA_MAILBOX", os.getenv("MS_USER_EMAIL"))
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]


def get_access_token() -> str:
    app = ConfidentialClientApplication(
        client_id=CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", "Failed to get token"))
    return result["access_token"]


def main() -> None:
    token = get_access_token()
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT00:00:00Z"
    )
    params = {
        "$filter": f"receivedDateTime ge {since}",
        "$orderby": "receivedDateTime desc",
        "$top": "25",
        "$select": "id,subject,receivedDateTime,from,toRecipients,isRead,hasAttachments",
    }
    response = requests.get(
        f"{GRAPH_BASE}/users/{DATA_MAILBOX}/messages",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params,
        timeout=30,
    )
    if not response.ok:
        print(response.text)
        response.raise_for_status()

    messages = response.json().get("value", [])
    print(f"Mailbox: {DATA_MAILBOX}")
    print(f"Recent messages since: {since}")
    print()
    for index, message in enumerate(messages, start=1):
        sender = (
            message.get("from", {})
            .get("emailAddress", {})
            .get("address", "(unknown)")
        )
        recipients = [
            recipient.get("emailAddress", {}).get("address", "")
            for recipient in message.get("toRecipients", [])
        ]
        print(f"{index}. {message.get('receivedDateTime', '')}")
        print(f"   From: {sender}")
        print(f"   To: {', '.join(r for r in recipients if r)}")
        print(f"   Read: {message.get('isRead')}  Attachments: {message.get('hasAttachments')}")
        print(f"   Subject: {message.get('subject', '')}")
        print()


if __name__ == "__main__":
    main()
