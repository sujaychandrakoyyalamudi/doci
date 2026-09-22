import json

from fastapi import BackgroundTasks

from doci.config import Settings
from doci.models import uid


class Queue:
    def __init__(self, settings: Settings, workflow):
        self.settings, self.workflow = settings, workflow

    def dispatch(self, run_id: str, background: BackgroundTasks):
        if self.settings.queue_backend == "local":
            background.add_task(self.workflow.execute, run_id)
            return
        from google.cloud import tasks_v2
        from google.protobuf import duration_pb2

        client = tasks_v2.CloudTasksClient()
        parent = client.queue_path(
            self.settings.google_cloud_project,
            self.settings.google_cloud_location,
            self.settings.cloud_tasks_queue,
        )
        # Each dispatch may retry. The workflow lock and action uniqueness ensure idempotency.
        client.create_task(
            request={
                "parent": parent,
                "task": {
                    "name": f"{parent}/tasks/review-{run_id}-{uid()}",
                    "dispatch_deadline": duration_pb2.Duration(seconds=600),
                    "http_request": {
                        "http_method": tasks_v2.HttpMethod.POST,
                        "url": f"{self.settings.worker_url.rstrip('/')}/api/internal/runs/{run_id}",
                        "headers": {"Content-Type": "application/json"},
                        "body": json.dumps({"run_id": run_id}).encode(),
                        "oidc_token": {
                            "service_account_email": self.settings.task_service_account,
                            "audience": self.settings.worker_url,
                        },
                    },
                },
            }
        )
