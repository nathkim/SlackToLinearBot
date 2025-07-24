from google.cloud import bigquery

def upload_to_bigquery(project_id: str, dataset_id: str, table_id: str, data: list[dict]) -> None:
    try:
        client = bigquery.Client(project=project_id)
    
        table_ref = client.dataset(dataset_id).table(table_id)
    
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND
        )
    
        job = client.load_table_from_json(data, table_ref, job_config=job_config)
    
        job.result()  # Wait for the job to complete
        print(f"Uploaded {len(data)} rows to {table_id} in dataset {dataset_id}.")
    except Exception as e:
        print(f"An error occurred while uploading to BigQuery: {e}")