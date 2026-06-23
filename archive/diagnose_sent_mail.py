"""Print recent sent-message metadata for the pipeline mailbox."""

import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from msal import ConfidentialClientApplication


load_dotenv()

TENANT_ID = os.getenv("AZURE_TENANT_ID")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
SEND_MAILBOX = os.getenv("SEND_MAILBOX", os.getenv("MS_USER_EMAIL"))
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
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%dT00:00:00Z"
    )
    params = {
        "$filter": f"sentDateTime ge {since}",
        "$orderby": "sentDateTime desc",
        "$top": "10",
        "$select": "subject,sentDateTime,toRecipients,hasAttachments",
    }
    response = requests.get(
        f"{GRAPH_BASE}/users/{SEND_MAILBOX}/mailFolders/SentItems/messages",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params,
        timeout=30,
    )
    if not response.ok:
        print(response.text)
        response.raise_for_status()

    print(f"Mailbox: {SEND_MAILBOX}")
    print(f"Recent sent messages since: {since}")
    print()
    for index, message in enumerate(response.json().get("value", []), start=1):
        recipients = [
            recipient.get("emailAddress", {}).get("address", "")
            for recipient in message.get("toRecipients", [])
        ]
        print(f"{index}. {message.get('sentDateTime', '')}")
        print(f"   To: {', '.join(r for r in recipients if r)}")
        print(f"   Attachments: {message.get('hasAttachments')}")
        print(f"   Subject: {message.get('subject', '')}")
        print()


if __name__ == "__main__":
    main()
