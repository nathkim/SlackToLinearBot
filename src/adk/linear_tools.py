import os
import requests
import json
import re
import vertexai
from google.cloud import pubsub_v1
from vertexai.preview.generative_models import GenerativeModel
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.cloud import bigquery
import google.auth
from dotenv import load_dotenv
load_dotenv()

from .get_secrets import get_secret

PROJECT_ID = "szns-tpm-bot"
LOCATION = "us-central1"
SUBSCRIPTION_ID = "eng-standup-sub"

LINEAR_API_KEY = os.getenv("LINEAR_API_KEY") # MAKE SURE TO CHANGE THIS IN SECRETS MANAGER
BASE_URL = "https://api.linear.app/graphql"

headers = {
    "Authorization": LINEAR_API_KEY,
    "Content-Type": "application/json"
}

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
# SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE") # Only for local testing, remove in production

def get_credentials():
    # --- Should be added in production when service account is attached to project ---
    credentials, _ = google.auth.default(scopes=SCOPES)
    return credentials
    # --- Should be removed in production ---
    '''
    return ServiceAccountCredentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes = SCOPES
    )
    '''

def get_issues() -> dict:
    """Fetches all Linear issues across the workspace including assignees' names and emails."""
    query = """
    query {
        issues {
            nodes {
                id
                title
                description
                state {
                    name
                }
                assignee {
                    name
                    email
                }
                team {
                    id
                    name
                }
            }
        }
    }
    """
    response = requests.post(BASE_URL, json={"query": query}, headers=headers)
    if response.status_code == 200:
        return {
            "status": "success",
            "issues": response.json()["data"]["issues"]["nodes"]
        }
    else:
        return {
            "status": "error",
            "error_message": f"{response.status_code}: {response.text}"
        }

def extract_json_block(text: str) -> list:
    # Remove markdown-style backticks
    text = re.sub(r"```json|```", "", text).strip()
    
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"Error decoding JSON from text: {text}")
        return []

def send_to_slack(task: dict, data: dict) -> bool:
    exp_status = data.get("status") if data.get("status") else None
    cur_status = task.get("status") if task.get("status") else None
    if exp_status is None or cur_status is None:
        return True    
    return exp_status.strip().lower() != cur_status.strip().lower()

def format_payload(task: dict, data: dict, matched: bool) -> dict:
    name = data.get("name", "unidentified")
    
    raw_status = data.get("status", "")
    exp_status = raw_status.strip().lower() if raw_status else ""
    
    cur_status_raw = task.get("status", "")
    cur_status = cur_status_raw.strip().lower() if cur_status_raw else None

    title = task.get("matched_issue_title")

    return {
        "name": name,
        "cur_status": cur_status if matched else None,
        "exp_status": exp_status,
        "title": title if matched else data.get("task")
    }

def input_for_slack(data: dict) -> dict:
    task_list = extract_json_block(str(data))
    for task in task_list:
        matched = task.get("matched_issue_title")
        if matched and send_to_slack(task, data):
            return format_payload(task, data, matched=True)
        elif not matched:
            return format_payload(task, data, matched=False)
    return {}
    
def callback(message: pubsub_v1.subscriber.message.Message):
    raw_data = message.data.decode('utf-8')
    data = json.loads(raw_data)
    input_for_slack(data)
    message.ack()

def compare(task_data: dict) -> dict:
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel("gemini-2.0-flash-lite")

    issues_data = get_issues()
    if issues_data is None or issues_data.get("status") != "success":
        return {"error": "Failed to fetch Linear issues."}

    linear_issues = issues_data["issues"]

    prompt = f"""
        You are an AI assistant at SZNS. Compare standup-reported tasks with current Linear issues.

        Match tasks based on their meaning (not just keywords). Only return deliverable tasks, not meetings or updates. 

        Return a list of tasks with the following fields:
        - name: Name of the person responsible for the task (get from Standup Tasks input)
        - task: Description of the task (get from Standup Tasks input)
        - cur_status: The current status of the *matched Linear issue*. If no Linear issue is matched, this should be null. (Get this from the 'state.name' field of the matched Linear issue in the 'Current Linear Issues' input.)
        - exp_status: The expected status given from the 'status' key in the 'Standup Task' input.
        - matched_issue_title: Title of the matched Linear issue (get from Current Linear Issues input). If no Linear issue is matched, this should be null.

        If there are no common keywords between the task description from the Standup Tasks input and the titles of the Linear issues, do not match the task to any Linear issue (leave status and matched_issue_title null).

        Format:
          {{
            "name": "Nam",
            "task": "Start building Linear integration",
            "cur_status": "To Do",
            "exp_status": "In Progress",
            "matched_issue_title": "Implement Linear integration"
          }}
        

        Standup Task:
        {json.dumps(task_data, indent=2)}

        Current Linear Issues:
        {json.dumps(linear_issues, indent=2)}
    """
    response = model.generate_content(prompt)
    return response.text

def get_state_id_by_name(state_name: str) -> str:
    """Fetches the ID of a Linear workflow state by its name."""
    query = """
    query {
        workflowStates {
            nodes {
                id
                name
            }
        }
    }
    """
    response = requests.post(BASE_URL, json={"query": query}, headers=headers)
    if response.status_code == 200:
        states = response.json().get("data", {}).get("workflowStates", {}).get("nodes", [])
        for state in states:
            if state["name"].strip().lower() == state_name.strip().lower():
                return state["id"]
    return None

def get_team_id_by_name(team_name: str) -> str:
    """Fetches the ID of a Linear team by its name."""
    query = """
    query {
        teams {
            nodes {
                id
                name
            }
        }
    }
    """
    response = requests.post(BASE_URL, json={"query": query}, headers=headers)

    if response.status_code == 200:
        teams = response.json().get("data", {}).get("teams", {}).get("nodes", [])
        for team in teams:
            if team["name"].strip().lower() == team_name.strip().lower():
                return team["id"]
    return None

def update_linear_issue(task_data: dict) -> dict:
    # get issue ID from title
    issues = get_issues()["issues"] 
    matched_issue = next((issue for issue in issues if issue["title"].strip().lower() == task_data["title"].strip().lower()), None)
    if not matched_issue:
        return {"status": "error", "message": "No matching issue found in Linear."}
    
    state_id = get_state_id_by_name(task_data.get("status", ""))
    if not state_id:
        return {"status": "error", "message": "No matching status found in Linear."}
    
    team_id = matched_issue.get("team", {}).get("id")
    if not team_id:
        return {"status": "error", "message": "No team ID found for the matched issue."}

    # Update the status of the matched Linear issue
    mutation = """
    mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
        issueUpdate(id: $id, input: $input) {
            success
            issue {
                id
                title
                state {
                    name
                }
            }
        }
    }
    """
    
    variables = {
        "id": matched_issue["id"],
        "input": {
            "stateId": state_id,
            "teamId": team_id, 
        }
    }

    response = requests.post(BASE_URL, json={"query": mutation, "variables": variables}, headers=headers, timeout=15)
    if response.status_code == 200:
        result = response.json().get("data", {}).get("issueUpdate", {})
        if result.get("success"):
            return {
                "status": "success",
                "message": f"Issue '{result['issue']['title']}' updated to status '{task_data['status']}'"
            }
        else:
            return {"status": "error", "message": "Failed to update issue."}


def update_linear_priority(task_data: dict) -> dict:
    """
    Update the priority of a Linear issue based on its title.
    task_data = {
        "title": "<title of the issue>",
        "priority": 1  # integer between 0 and 4
    }
    """
    issues = get_issues().get("issues", [])
    matched_issue = next((issue for issue in issues if issue["title"].strip().lower() == task_data["title"].strip().lower()), None)
    
    if not matched_issue:
        return {"status": "error", "message": "No matching issue found in Linear."}

    priority = task_data.get("priority")
    if priority not in [0, 1, 2, 3, 4]:
        return {"status": "error", "message": f"Invalid priority level: {priority}"}

    mutation = """
    mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
        issueUpdate(id: $id, input: $input) {
            success
            issue {
                id
                title
                priority
            }
        }
    }
    """

    variables = {
        "id": matched_issue["id"],
        "input": {
            "priority": priority
        }
    }

    response = requests.post(BASE_URL, json={"query": mutation, "variables": variables}, headers=headers)
    
    if response.status_code == 200:
        result = response.json().get("data", {}).get("issueUpdate", {})
        if result.get("success"):
            return {
                "status": "success",
                "message": f"Issue '{result['issue']['title']}' updated to priority {result['issue']['priority']}"
            }
        else:
            return {"status": "error", "message": "Failed to update priority."}
    else:
        return {"status": "error", "message": f"Error {response.status_code}: {response.text}"}

def match_issue(task_title: str) -> str:
    """
    Uses Gemini to find the most semantically similar Linear issue to the given task_title.
    Returns the matched issue object or None.
    """
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel("gemini-2.0-flash-lite")

    # Get all Linear issues
    issues_data = get_issues()
    if not issues_data or issues_data.get("status") != "success":
        return None

    issues = issues_data["issues"]

    prompt = f"""
        You are a task matching assistant.

        Given the task: "{task_title}"

        Select the *one* most semantically similar issue from the list below.

        Respond ONLY with the exact title of the best-matched issue.

        Issues:
        {json.dumps([issue["title"] for issue in issues], indent=2)}
    """

    response = model.generate_content(prompt)
    best_match_title = response.text.strip().strip('"')

    return best_match_title

def handle_dm_update(text: str) -> dict:
    """
    Parses a natural language DM to extract task title and new status,
    then updates the issue in Linear.
    """
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel("gemini-2.0-flash-lite")
    issues = get_issues().get("issues", [])

    prompt = f"""
        You are a task update assistant. Given a message from Slack, extract:
        - title: the exact title of the Linear issue to update
        - status: the new status to set

        Respond ONLY with a JSON object with keys: "title" and "status".
        If the title doesn't exactly match any existing Linear issue, use the closest match.

        Slack message:
        \"{text}\"

        Existing Linear issues:
        {json.dumps([issue["title"] for issue in issues], indent=2)}
    """

    response = model.generate_content(prompt)
    response = extract_json_block(response.text.strip())

    try:
        return update_linear_issue(response)
    except Exception as e:
        return {"status": "error", "message": f"Gemini failed to parse task: {e}\nRaw: {response.text}"}

def format_issue_list(issues: list[dict]) -> str:
    max_issues = 15
    displayed = issues[:max_issues]
    lines = [
        f"- *{issue['title']}* ({issue['status']}) – {issue['assignee']} [Priority: {issue['priority']}]"
        for issue in displayed
    ]
    if len(issues) > max_issues:
        lines.append("…and more.")
    return "\n".join(lines)


def list_linear_issues() -> list[dict]:
    """
    Returns a list of Linear issues with metadata:
    title, status, assignee, priority (0-4), and issue link.
    """
    issues_data = get_issues()
    if issues_data.get("status") != "success":
        return []

    issues = issues_data["issues"]
    issue_list = []
    for issue in issues[:15]:
        issue_list.append({
            "title": issue["title"],
            "status": issue["state"]["name"] if issue.get("state") else None,
            "assignee": issue["assignee"]["name"] if issue.get("assignee") else "Unassigned",
            "priority": issue.get("priority", "N/A"),
            "url": f"https://linear.app/issue/{issue['id']}"
        })

    if len(issues) > 15:
        issue_list.append({
            "title": "...and more",
            "status": None,
            "assignee": None,
            "priority": None,
            "url": None
        })

    return issue_list

def run_bigquery_query(query: str) -> dict:
    try:
        credentials = get_credentials()
        client = bigquery.Client(credentials=credentials, project=PROJECT_ID)
        query_job = client.query(query)
        rows = query_job.result()
        return {"status": "success", "rows": [dict(row) for row in rows]}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def handle_metrics(text: str) -> dict:
    """
    Returns metrics data from BQ by translating natural language to SQL query 
    """
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel("gemini-2.0-flash-lite")
    issues = get_issues().get("issues", [])

    prompt = f"""
        You are a BigQuery SQL query generator assistant for the 'szns-tpm-bot' system. Your goal is to convert user requests received from Slack messages into valid and efficient BigQuery SQL queries.
        You have access to the following tables in the `linear_metrics_dev` dataset:

        **Table: `linear_tasks`**
        * `ingestion_timestamp` (TIMESTAMP)
        * `completed_at` (TIMESTAMP)
        * `created_at` (TIMESTAMP)
        * `status` (STRING)
        * `issue_title` (STRING)
        * `owner_name` (STRING)
        * `issue_id` (STRING)

        **Table: `linear_tasks_view`**
        * `ingestion_timestamp` (TIMESTAMP)
        * `owner_name` (STRING)
        * `status` (STRING)
        * `issue_count` (INTEGER)

        **Table: `avg_done_tasks_view`**
        * `ingestion_timestamp` (TIMESTAMP)
        * `owner_name` (STRING)
        * `avg_lead_time` (FLOAT)
        * `total_issues_in_avg` (INTEGER)

        ---

        **Instructions:**

        1.  **Strictly output only the SQL query.** Do not include any explanations, conversational text, or markdown code blocks for the query (i.e., no ````sql`).
        2.  All table references must be fully qualified: `linear_metrics_dev.<table_name>`.
        3.  **Table Selection Logic:**
            * If the request is about **average time spent on tasks** (e.g., "average lead time", "how long do tasks take"), use the **`avg_done_tasks_view`** table.
            * If the request is specifically about **task counts or statuses aggregated by owner/status** (e.g., "number of tasks per status", "tasks by owner"), use the **`linear_tasks_view`** table.
            * For **all other requests** involving Linear tasks (e.g., fetching individual task details, filtering by title, creation/completion dates), use the **`linear_tasks`** table.
        4.  If a request explicitly asks for data based on a specific `ingestion_timestamp`, prefer filtering by that. If not specified, and relevant, consider using `WHERE ingestion_timestamp = (SELECT MAX(ingestion_timestamp) FROM linear_metrics_dev.<table_name>)` to get the latest snapshot of data, but only if the user query implies recent data.
        5.  If the request is unclear or cannot be translated into a SQL query given the available tables, output: `INVALID_QUERY`.

        ---

        **Examples:**

        **User Input:** "How many tasks are in 'In Progress' status for each owner?"
        **Expected Output (No markdown, just SQL):**
        SELECT owner_name, issue_count FROM `linear_metrics_dev.linear_tasks_view` WHERE status = 'In Progress' GROUP BY owner_name

        **User Input:** "What's the average time taken to complete tasks by Alice?"
        **Expected Output (No markdown, just SQL):**
        SELECT avg_lead_time FROM `linear_metrics_dev.avg_done_tasks_view` WHERE owner_name = 'Alice'

        **User Input:** "Give me the titles and statuses of all tasks created this week."
        **Expected Output (No markdown, just SQL):**
        SELECT issue_title, status FROM `linear_metrics_dev.linear_tasks` WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)

        **User Input:** "Find the task with ID 'LIN-123'."
        **Expected Output (No markdown, just SQL):**
        SELECT * FROM `linear_metrics_dev.linear_tasks` WHERE issue_id = 'LIN-123'

        **User Input:** "What is the team's average bug resolution time?"
        **Expected Output (No markdown, just SQL):**
        INVALID_QUERY

        ---

        Slack Message:
        \"{text}\"
    """

    response = model.generate_content(prompt)
    sql_query = response.text.strip()
    sql_query = re.sub(r"```sql|```", "", sql_query).strip()
    result = run_bigquery_query(sql_query)
    return result

if __name__ == '__main__':
    print("Test")