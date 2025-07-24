import os
import json
import uuid
import re
import requests
import vertexai
from datetime import datetime, timezone

from slack_sdk import WebClient
from slack_bolt import App
from vertexai.generative_models import GenerativeModel
from google.cloud import pubsub_v1, firestore
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request, make_response

from get_secrets import get_secret
# from adk.linear_tools import update_linear_issue # TODO: add back, removed for cloud run debugging

# --- Config constants ---
PROJECT_ID = "szns-tpm-bot"
LOCATION = "us-central1"
TOPIC_ID = "eng-standup"
SLACK_CHANNEL_NAME = "team-standups"
ADK_BASE_URL = "https://adk-service-668646793196.us-central1.run.app"
ROUTING_AGENT_NAME = "adk"

# --- Secrets ---
SLACK_BOT_TOKEN = get_secret("SLACK_BOT_TOKEN", PROJECT_ID)
SLACK_APP_TOKEN = get_secret("SLACK_APP_TOKEN", PROJECT_ID)
SLACK_SIGNING_SECRET = get_secret("SLACK_SIGNING_SECRET", PROJECT_ID)
TPM_BOT_USER_ID = get_secret("TPM_BOT_USER_ID", PROJECT_ID)

# --- Slack + Firestore Setup ---
app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
client = WebClient(SLACK_BOT_TOKEN)
db = firestore.Client()
headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

handler = SlackRequestHandler(app)
flask_app = Flask(__name__)

# Is this needed?
def get_service_account_credentials(project_id=PROJECT_ID, secret_id="service-account-key"):
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    key_data = response.payload.data.decode("utf-8")

    service_account_info = json.loads(key_data)
    credentials = ServiceAccountCredentials.from_service_account_info(service_account_info)
    return credentials

# Extract JSON block from Gemini response
def extract_json_block(text):
    """ """
    # Remove markdown-style backticks
    text = re.sub(r"```json|```", "", text).strip()

    # Try to locate the JSON array
    match = re.search(r"\[\s*{.*}\s*\]", text, re.DOTALL)
    return match.group(0) if match else None

# Publish data to Google Cloud Pub/Sub
def publish_to_pubsub(project_id, topic_id, data):
    credentials = get_service_account_credentials()
    publisher = pubsub_v1.PublisherClient(credentials=credentials)
    topic_path = publisher.topic_path(project_id, topic_id)
    data = json.dumps(data).encode("utf-8")
    future = publisher.publish(topic_path, data=data)
    print(f"Published to {topic_id}: {data}")
    return future.result()

def get_today():
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()

# Function to get channel ID by name
def get_channel_id(CHANNEL_NAME):
    url = "https://slack.com/api/conversations.list"

    response = requests.get(url, headers=headers)
    data = response.json()
    if not data.get("ok"):
        raise Exception(f"Error fetching channels: {data.get('error', 'Unknown error')}")
    
    for channel in data.get("channels", []):
        if channel.get("name") == CHANNEL_NAME:
            return channel["id"]
    return None

def load_prompt(author, message):
    with open("slack-data/prompt.txt", "r") as file:
        prompt_template = file.read()
    return prompt_template.format(author=author, message=message)

def get_task_list(message, author="Unknown"):
  # Vertex AI setup
  credentials = get_service_account_credentials()
  vertexai.init(project=PROJECT_ID, location=LOCATION, credentials=credentials)
  model = GenerativeModel("gemini-2.0-flash-lite")

  # Extract tasks/owners using Gemini
  prompt = load_prompt(author, message)
  response = model.generate_content(prompt)
  return response.text

# Get channel ID TODO: Is this needed?
channel_id = get_channel_id(SLACK_CHANNEL_NAME)
if not channel_id:
    raise Exception(f"Channel '{SLACK_CHANNEL_NAME}' not found.")

# Get name of message author (to replace author ID in data)
def get_message_author(user_id):
    url = "https://slack.com/api/users.info"
    params = {"user": user_id}
    response = requests.get(url, headers=headers, params=params)
    return response.json().get("user", {}).get("name", user_id)

def buildRequestJson(agentName, user_id, session_id, user_message) -> dict: # Already in agent.py, import or combine?
    new_message = {"role": "User", "parts": [{"text": user_message}]}
    request_json = {
        "appName": agentName,
        "userId": user_id,
        "sessionId": session_id,
        "newMessage": new_message
    }
    return request_json

def call_adk_with_dm(user_message: str, user_id: str = "user") -> str:
    """
    Sends a user message to the ADK routing agent via /run_sse and returns final output text.
    """
    session_id = str(uuid.uuid4())
    session_url = f"{ADK_BASE_URL}/apps/adk/users/{user_id}/sessions/{session_id}"
    headers = {"Content-Type": "application/json"}

    # Create session
    try:
        session_response = requests.post(session_url, headers=headers)
        session_data = session_response.json()
        if not session_data.get("id"):
            print(f"[ADK ERROR] Session creation failed. Raw: {session_data}")
            return "Could not create ADK session."
    except Exception as e:
        print(f"[ADK ERROR] Session request failed: {e}")
        return "ADK session request failed."

    payload = buildRequestJson(ROUTING_AGENT_NAME, user_id, session_id, user_message)
    sse_url = f"{ADK_BASE_URL}/run_sse"

    collected_text = ""
    try:
        with requests.post(sse_url, data=json.dumps(payload), headers=headers, stream=True) as resp:
            for line in resp.iter_lines(decode_unicode=True):
                if line and line.startswith("data: "):
                    try:
                        raw_json = line[len("data: "):]
                        parsed = json.loads(raw_json)
                        content = parsed.get("content", {})
                        parts = content.get("parts", [])
                        for part in parts:
                            if "text" in part:
                                collected_text += part["text"]
                    except Exception as e:
                        print(f"[ADK ERROR] Chunk parse error: {e}")
    except Exception as e:
        print(f"[ADK ERROR] SSE stream failed: {e}")
        return "ADK stream failed."

    return collected_text.strip() if collected_text else "No output from ADK."

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

@app.event("reaction_added")
def handle_reaction_added(event, say, client):
    print("✅ Reaction event received:", json.dumps(event, indent=2))
    ts = event["item"]["ts"]
    reaction = event["reaction"]
    channel_id = event["item"]["channel"]

    update_info = load_pending_update(ts)
    if update_info:
        title = update_info["title"]

        if reaction == "+1":
            say(channel=channel_id, text=f"👍 Approved! Proceeding with Linear update for: {title}") # Debug message, remove when deployed unless we want to implement
            client.chat_delete(channel=channel_id, ts=ts)

            # --- TODO: MAKE HELPER METHOD ---
            linear_payload = {
                "title": update_info.get("title"),
                "status": update_info.get("exp_status")
            }
            # print(update_linear_issue(linear_payload)) # Print statement for debugging

            delete_pending_update(ts) # TODO: Delete this line in production (Dan said keep all logs)

        elif reaction == "-1":
            # Try deleting
            client.chat_delete(channel=channel_id, ts=ts)
            delete_pending_update(ts)

        else:
            print(f"Reaction {reaction} received but no action taken for: {title}")

@app.event("message")
def handle_message_posted(event, say):
    print("Message event received:", json.dumps(event, indent=2)) # Debug (Delete later)
    channel_id = event.get("channel", "")
    user_id = event.get("user", "")
    text = event.get("text", "")

    # Ignore bot messages
    if user_id == TPM_BOT_USER_ID:
        print("Skipping message from TPM bot.")
        return

    # DM
    if channel_id.startswith("D"):
        # Call ADK with message
        adk_response = call_adk_with_dm(text, user_id)
        client.chat_postMessage(channel=channel_id, text=adk_response)

    # Channel message
    else:
        print(f"Channel message in {channel_id} from {author_name}: {text}")
        author_name = get_message_author(user_id).replace(".", " ")
        gemini_output = get_task_list(text, author=author_name)
        cleaned = extract_json_block(gemini_output)

        if cleaned:
            try:
                tasks = json.loads(cleaned)
                for task in tasks:
                    publish_to_pubsub(PROJECT_ID, TOPIC_ID, task)
            except json.JSONDecodeError as e:
                print(f"Error decoding JSON from Gemini output for {author_name}: {e}")
        else:
            print("No valid JSON array found in Gemini output for {author_name}.") 

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))