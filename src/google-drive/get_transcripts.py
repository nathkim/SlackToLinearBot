import os, re
import google.auth

from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from get_secrets import get_secret

# Env vars for local testing (remove in production)
from dotenv import load_dotenv
load_dotenv()

FOLDER_ID = "1UBqjoRGoJLUX3aLn6wCjmsYkZAG7E88J" # 'Meet Recordings' folder
# SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE") # Only for local testing, remove in production

# Scopes (permissions) for Google API access
SCOPES = [  
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/documents.readonly'
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

def list_gdocs_in_folder(service, folder_id):
    """Lists all gDoc files in the specified gDrive folder """
    q = f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.document'"
    resp = service.files().list(q=q, fields="files(id,name)").execute()
    return resp.get('files', [])

def extract_text(body):
    """Extracts and cleans text from the given gDoc"""
    lines = []
    for elem in body.get('content', []):
        para = elem.get('paragraph')
        if not para:
            continue
        for e in para.get('elements', []):
            tr = e.get('textRun')
            if tr:
                content = tr.get('content', '').strip()
                content = re.sub(r'[\x00-\x1F\u200b\uFEFF\xa0\u2028\u2029\u200e\u200f\u202a-\u202e]', '', content)

                # Removes unwanted content from transcript (date, title, timestamps, and Gemini-generated text)
                if not content:
                    continue
                if content.lower().endswith('- transcript'):
                    continue
                if "transcription ended" in content.lower():
                    return '\n'.join(lines)
                if "editable transcript" in content.lower():
                    continue

                content = re.sub(r'\b\d{1,2}:\d{2}(?::\d{2})?\s?(AM|PM)?\b', '', content)
                content = re.sub(r'\[\d{2}:\d{2}(?::\d{2})?\]', '', content)
                lines.append(content.strip())
    return '\n'.join(line for line in lines if line)

def get_transcript_docs():
    """Returns a list of (doc_id, doc_name, transcript_text) items from the transcript tab of each doc"""
    creds = get_credentials()
    drive = build('drive', 'v3', credentials=creds)
    docs = build('docs', 'v1', credentials=creds)

    results = []
    try:
        files = list_gdocs_in_folder(drive, FOLDER_ID)
        for f in files:
            doc = docs.documents().get(documentId=f['id'], fields="title,tabs").execute()
            tabs = doc.get('tabs', [])
            if len(tabs) < 2:
                continue
            body = tabs[1].get('documentTab', {}).get('body', {})
            transcript = extract_text(body)
            if transcript:
                results.append((f['id'], f['name'], transcript))
    except HttpError as e:
        print("An error occurred:", e)
    return results

# Testing script
if __name__ == '__main__':
    for doc_id, name, text in get_transcript_docs():
        print(f"\nTranscript from: {name}")
        print(text[:300] + "...\n")