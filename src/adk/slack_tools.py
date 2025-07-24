import os, requests, json
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import uuid
from .name_email_map import NAME_EMAIL_MAP
from .linear_tools import update_linear_issue
from .get_secrets import get_secret
from dotenv import load_dotenv
load_dotenv()

from google.cloud import firestore

# Initialize Firestore client
db = firestore.Client()

PROJECT_ID = "szns-tpm-bot"
LOCATION = "us-central1"

# Slack tokens
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
SLACK_CHANNEL_ID = "C092ETPPY1F" # For testing, CHANGE THIS

app = App(token=SLACK_BOT_TOKEN)

# --- FIREBASE HELPER METHODS ---
# Save pending update
def save_pending_update(ts: str, data: dict):
    doc_ref = db.collection("pending_updates").document(ts)
    doc_ref.set(data)

# Load pending update
def load_pending_update(ts: str):
    doc_ref = db.collection("pending_updates").document(ts)
    doc = doc_ref.get()
    if doc.exists:
        return doc.to_dict()
    else:
        return None

# Delete pending update
def delete_pending_update(ts: str):
    doc_ref = db.collection("pending_updates").document(ts)
    doc_ref.delete()


def get_email_for_name(name: str) -> str | None:
    """
    Look up email by first name (case insensitive).
    """
    if name.lower() == "unidentified":
        return name.lower()
    return NAME_EMAIL_MAP.get(name.lower())

def get_slack_user_id(email: str) -> str | None:
    """
    Given an email, use Slack API to find user ID.
    """
    if email.lower() == "unidentified":
        return email.lower()
    
    slack_url = "https://slack.com/api/users.lookupByEmail"
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}"
    }
    params = {
        "email": email
    }

    response = requests.get(slack_url, headers=headers, params=params)
    data = response.json()

    if response.status_code == 200 and data.get("ok"):
        return data["user"]["id"]
    else:
        print(f"❌ Could not find Slack user ID for {email}: {data.get('error')}")
        return None
    
def post_approval_message(update_data: dict) -> dict:
    """
    update_data: {
        'name': '<Name>',
        'cur_status': '<Current status>',
        'exp_status': '<Expected status>',
        'title': '<Linear issue title>'
    }
    """
    # Terminates because no matching task - TODO create task in Linear instead of terminating
    if update_data.get("cur_status") is None or update_data.get("title") is None:
        return {"status": "skipped", "reason": "Task doesn't exist"}
    
    # Terminates if Linear is already updated
    if update_data.get("cur_status").lower() == update_data.get("exp_status").lower():
        return {"status": "skipped", "reason": "Linear is up to date"}

    update_id = str(uuid.uuid4())

    name = update_data.get("name", "Unidentified")
    title = update_data.get("title", "Unidentified")
    cur_status = update_data.get("cur_status", "Unidentified")
    exp_status = update_data.get("exp_status", "Unidentified")

    # --- STANDUP CHANNEL MESSAGE ---
    if name.lower() == "unidentified":
        text = (
            f"Hello team! :wave: I want to update:\n"
            f"*{title}* on Linear\n"
            f"from `{cur_status.title()}` → `{exp_status.title()}`\n"
            "React with :+1: to approve update, or :-1: to cancel"
        )
        slack_url = "https://slack.com/api/chat.postMessage"
        payload = {
            "channel": SLACK_CHANNEL_ID,
            "text": text
        }
        headers = {
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8"
        }

        response = requests.post(slack_url, headers=headers, json=payload)
        data = response.json()

        if response.status_code == 200 and data.get("ok"):
            ts = data["ts"]
            save_pending_update(ts, {
                "update_id": update_id,
                "title": title,
                "exp_status": exp_status
            })
            return {"status": "success", "ts": ts}
        else:
            return {"status": "error", "error_message": data.get("error", response.text)}
    
    # --- DM ON SLACK ---
    else:
        email = NAME_EMAIL_MAP.get(name.lower())
        first_name = name.split(" ")[0].title()
        text = (
            f"Hi {first_name.title()}! I want to update:\n"
            f"*'{title}'* on Linear\n"
            f"from `{cur_status.title()}` → `{exp_status.title()}`\n"
            "React with :+1: to approve, or :-1: to reject."
        )

        if not email:
            return {"status": "error", "error_message": f"No email found for name: {name}"}

        slack_user_id = get_slack_user_id(email)

        if not slack_user_id:
            return {"status": "error", "error_message": f"Slack user ID not found for email: {email}"}

        slack_url = "https://slack.com/api/chat.postMessage"
        payload = {
            "channel": slack_user_id,
            "text": text
        }
        headers = {
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8"
        }

        response = requests.post(slack_url, headers=headers, json=payload)
        data = response.json()

        if response.status_code == 200 and data.get("ok"):
            ts = data["ts"]
            save_pending_update(ts, {
                "update_id": update_id,
                "title": title,
                "exp_status": exp_status
            })
            return {"status": "success", "ts": ts}
        else:
            return {"status": "error", "error_message": data.get("error", response.text)}