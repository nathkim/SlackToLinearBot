import json
import vertexai
import re
import os
import time

from .get_transcripts import get_transcript_docs
from google.cloud import pubsub_v1
from vertexai.preview.generative_models import GenerativeModel
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from dotenv import load_dotenv
from name_email_map import NAME_EMAIL_MAP
import google.auth
from get_secrets import get_secret
script_dir = os.path.dirname(__file__)
prompt_path = os.path.join(script_dir, "summarize_prompt.txt")

load_dotenv()
PROJECT_ID = "szns-tpm-bot"
LOCATION = "us-central1"
TOPIC_ID = "eng-standup"
PROCESSED_FOLDER_ID = "152F2f6iryx72b0oERZ-kAG9MAaZBQD2W"
# SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE") # Only for local testing, remove in production

# Permissions for Google API access
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly"
]

def get_credentials():
    # --- Should be added in production when service account is attached to project ---
    credentials, _ = google.auth.default(scopes=SCOPES)
    return credentials

    '''
    # --- Should be removed in production ---
    return ServiceAccountCredentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes = SCOPES
    )
    '''

def match_name_with_gemini(ai_name: str) -> str:
    """
    Uses Gemini to match a possibly incorrect AI-provided name to your known name keys.
    Returns the matched canonical name, or "No Match" if not found.
    """
    # Get keys from your permanent mapping
    name_list = list(NAME_EMAIL_MAP.keys())

    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel("gemini-2.0-flash-lite")

    prompt = f"""
        You are an assistant at SZNS. You need to match a possibly misspelled or incomplete first name from a standup summary to a known list of teammate names.

        Your task:
        - If no clear match is possible leave name as "unidentified" instead of forcing a match
        - Match the given name (even if first name or last name only) to the closest full name from the list below

        ### Name from summary:
        "{ai_name}"

        ### Known valid names:
        {', '.join(name_list)}

        ### Output:
        Only return the best matching name or "Unidentified". Do not explain.
    """

    response = model.generate_content(prompt)
    matched_name = response.text.strip().lower()

    return matched_name

def update_names_in_summary(summary: list[dict]) -> list[dict]:
    """
    Given a list of {'person': ..., 'task': ...} dicts, updates 'person' field using Gemini name matching.
    Returns updated list.
    """
    updated_summary = []

    for entry in summary:
        raw_name = entry.get("name", "")
        matched_name = match_name_with_gemini(raw_name)

        if matched_name != "unidentified":
            entry["name"] = matched_name
        else:
            entry["name"] = "Unidentified"

        updated_summary.append(entry)

    return updated_summary

def summarize_transcript(transcript_text):
    """ """
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel("gemini-2.0-flash-lite")

    script_dir = os.path.abspath(os.path.dirname(__file__))
    prompt_path = os.path.join(script_dir, "summarize_prompt.txt")
    # --- TODO: IMPROVE PROMPT IN summarize_prompt.txt ---
    with open(prompt_path, "r") as f:
        prompt_template = f.read()

    prompt = f"{prompt_template}\n{transcript_text}"

    response = model.generate_content(prompt)
    return response.text

def move_to_processed_folder(service, file_id, processed_folder_id):
    """ """
    file = service.files().get(fileId=file_id, fields="parents").execute()
    current_parents = ",".join(file.get("parents", []))
    service.files().update(
        fileId=file_id,
        addParents=processed_folder_id,
        removeParents=current_parents,
        fields="id, parents"
    ).execute()
    print(f"Successfully moved {file_id} to processed folder")

def extract_json_block(text):
    """ """
    text = re.sub(r"```json|```", "", text).strip()

    # Try to locate the JSON array
    match = re.search(r"\[\s*{.*}\s*\]", text, re.DOTALL)
    return match.group(0) if match else None

def publish_to_pubsub(project_id, topic_id, message_dict):
    """ """
    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(project_id, topic_id)
    data = json.dumps(message_dict).encode("utf-8")
    future = publisher.publish(topic_path, data=data)
    return future.result()

if __name__ == '__main__':
    creds = get_credentials()
    drive_service = build("drive", "v3", credentials=creds)

    # TODO fix authentication for service account. currently fails to be authenticated
    while True:
        try:
            docs = get_transcript_docs()
            
            if not docs:
                print("No new transcripts found.")
            else:
                for doc_id, name, text in docs:
                    print(f"\nSummarizing: {name}")
                    result = summarize_transcript(text)
                    print(result)  # Raw Gemini output

                    cleaned_json_text = extract_json_block(result)

                    if cleaned_json_text:
                        tasks_list = json.loads(cleaned_json_text)
                        updated_result = update_names_in_summary(tasks_list)

                        print(updated_result)  # After names updated

                        for task in updated_result:
                            publish_to_pubsub(PROJECT_ID, TOPIC_ID, task)
                    else:
                        print(f"No valid JSON array found in Gemini output for {name}")

                    move_to_processed_folder(drive_service, doc_id, PROCESSED_FOLDER_ID)

            # TODO figure out how often we want to check for docs
            time.sleep(60)

        except Exception as e:
            print(f"Error during loop: {e}")
            time.sleep(60) # TODO match with .sleep() above