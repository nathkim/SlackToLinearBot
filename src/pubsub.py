from flask import Flask, request
import base64, json, uuid, requests, re
import os

app = Flask(__name__)

ADK_BASE_URL = "https://adk-service-668646793196.us-central1.run.app"

def buildRequestJson(agentName, user_id, session_id, user_message) -> dict:
    return {
        "appName": agentName,
        "userId": user_id,
        "sessionId": session_id,
        "newMessage": {
            "role": "User",
            "parts": [{"text": user_message}]
        }
    }

@app.route("/", methods=["POST"])
def pubsub_handler():
    envelope = request.get_json()
    if not envelope or "message" not in envelope:
        return "Bad request: no Pub/Sub message received", 400

    try:
        pubsub_message = envelope["message"]
        payload = base64.b64decode(pubsub_message["data"]).decode("utf-8")
        data = json.loads(payload)
        print("Received message:", json.dumps(data, indent=2))

        # --- Create ADK session ---
        session_id = str(uuid.uuid4())
        user_id = "user"
        session_url = f"{ADK_BASE_URL}/apps/adk/users/{user_id}/sessions/{session_id}"
        headers = {"Content-Type": "application/json"}

        session_response = requests.post(session_url, headers=headers)
        session_data = session_response.json()
        if not session_data.get("id"):
            print(f"Failed to create session. Raw: {session_data}")
            return "Session creation failed", 500

        user_message_text = json.dumps(data, indent=2)
        sse_payload = buildRequestJson("adk", user_id, session_id, user_message_text)
        sse_url = f"{ADK_BASE_URL}/run_sse"

        collected_text = ""
        with requests.post(sse_url, data=json.dumps(sse_payload), headers=headers, stream=True) as resp:
            for line in resp.iter_lines(decode_unicode=True):
                if line and line.startswith("data: "):
                    raw_json = line[len("data: "):]
                    try:
                        parsed = json.loads(raw_json)
                        content = parsed.get("content", {})
                        parts = content.get("parts", [])
                        for part in parts:
                            if "text" in part:
                                collected_text = part["text"]
                    except Exception as e:
                        print(f"Error parsing chunk: {e}")

        print(f"ADK response: {collected_text.strip() if collected_text else 'No response'}")
        return "OK", 200

    except Exception as e:
        print(f"Exception while processing message: {e}")
        return "Error", 500
'''
import os
from google.cloud import pubsub_v1
from dotenv import load_dotenv
load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")
LOCATION = os.getenv("LOCATION")
SUBSCRIPTION_ID = os.getenv("SUBSCRIPTION_ID")

def callback(message: pubsub_v1.subscriber.message.Message):
    print(f"Received message: {message.data}")

    try:
        data = json.loads(message.data.decode("utf-8"))

        SESSION_ID = str(uuid.uuid4())
        user_id = "user"

        # --- Create ADK session ---
        ADK_SESSION_URL = f"{ADK_BASE_URL}/apps/adk/users/{user_id}/sessions/{SESSION_ID}"
        headers = {"Content-Type": "application/json"}

        session_response = requests.post(ADK_SESSION_URL, headers=headers)
        session_data = session_response.json()
        session_id = session_data.get("id")
        if not session_id:
            print(f"Failed to retrieve session ID. Raw: {session_data}")
            message.nack()
            return

        # Build user message text from your task data
        user_message_text = json.dumps(data, indent=2)

        # --- Prepare payload for /run_sse ---
        sse_payload = buildRequestJson("adk", user_id, session_id, user_message_text)
        SSE_URL = f"{ADK_BASE_URL}/run_sse"

        collected_text = ""

        try:
            with requests.post(SSE_URL, data=json.dumps(sse_payload), headers=headers, stream=True) as resp:
                for line in resp.iter_lines(decode_unicode=True):
                    if line and line.startswith("data: "):
                        raw_json = line[len("data: "):]
                        try:
                            parsed = json.loads(raw_json)
                            content = parsed.get("content", {})
                            parts = content.get("parts", [])
                            for part in parts:
                                if "text" in part:
                                    collected_text = part["text"]
                        except Exception as e:
                            print("Error parsing chunk:", e)
        except Exception as e:
            print(f"ADK SSE request failed: {e}")
            message.nack()
            return

        print(f"ADK response text: {collected_text.strip() if collected_text else 'No final text returned.'}")

        message.ack()

    except Exception as e:
        print(f"Exception during callback: {e}")
        message.nack()

def buildRequestJson(agentName, user_id, session_id, user_message) -> dict:
    new_message = {"role": "User", "parts": [{"text": user_message}]}
    request_json = {
        "appName": agentName,
        "userId": user_id,
        "sessionId": session_id,
        "newMessage": new_message
    }
    return request_json

if __name__ == "__main__":
    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(PROJECT_ID, SUBSCRIPTION_ID)
    # Subscribe and start streaming
    streaming_pull_future = subscriber.subscribe(subscription_path, callback=callback)
    try:
        streaming_pull_future.result()
    except KeyboardInterrupt:
        streaming_pull_future.cancel()
        streaming_pull_future.result()
'''