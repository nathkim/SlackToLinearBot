from metrics import percent_done, upload_to_bigquery
from adk.linear_tools import get_issues
import os
from datetime import datetime, timezone 
from google.cloud import bigquery
from dotenv import load_dotenv
load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID") 
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY")
BASE_URL = "https://api.linear.app/graphql"

METRICS_TABLE_SCHEMA = [
    bigquery.SchemaField("time", "TIMESTAMP"),
    bigquery.SchemaField("metric_name", "STRING"),
    bigquery.SchemaField("metric_value", "FLOAT"),
]

headers = {
    "Authorization": LINEAR_API_KEY,
    "Content-Type": "application/json"
}

def main():
    issues_list = get_issues()
    print("Fetched issues from Linear:")
    if issues_list["status"] == "error":
        print("Error fetching issues:", issues_list["error_message"])
        return
    else:
        issues = issues_list["issues"]
        for issue in issues:
            print(f"- {issue['title']} (ID: {issue['id']}, Status: {issue['state']['name']})")
            print("\nTotal issues fetched:", len(issues))
    
    done_percentage = percent_done(issues)
    print(f"\nPercentage of issues marked as 'Done': {done_percentage:.2f}%")

    timestamp = datetime.now(timezone.utc).isoformat()
    print(f"\nCurrent timestamp: {timestamp}")

    metric_data = [
        {
            "time": timestamp,
            "metric_name": "percent_done",
            "metric_value": done_percentage
        }
    ]

    try:
        print("\nUploading metrics to BigQuery...")
        upload_to_bigquery(
            project_id=PROJECT_ID,
            dataset_id="linear_metrics_dev",
            table_id="linear_metrics_summary",
            data=metric_data
        )
        print("Metrics uploaded successfully.")
    except Exception as e:
        print(f"Error uploading metrics to BigQuery: {e}")

if __name__ == "__main__":
    main()