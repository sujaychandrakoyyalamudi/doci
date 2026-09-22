# Observability and production monitoring

Doci uses LangSmith for agent execution traces and Google Cloud Monitoring for service health. Administrators can open both from **Monitoring** in the workspace. A case with a newly traced review also provides **Open trace in LangSmith** beneath its trace reference.

## What is collected

| Destination      | Signals                                                                                                                          |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| LangSmith        | Graph steps, model calls, execution timing, model/provider metadata, token usage, opaque review IDs, and numeric review feedback |
| Cloud Logging    | Structured request, queue, workflow, decision-submission, and telemetry-delivery events                                          |
| Cloud Trace      | Sampled HTTP and workflow spans, correlated with structured logs                                                                 |
| Cloud Monitoring | Request traffic/errors, latency, execution failures, task backlog, database health, uptime checks, and operational incidents     |

LangSmith estimates model cost when pricing is available for the recorded model. Cost estimates are not billing records.

Each graph execution or resumption gets a new trace UUID. `review_id` and `thread_id` group the executions of one review. Human approval waiting time is excluded from graph execution latency. Previously completed reviews are not retroactively uploaded.

## Content protection

Production tracing with this integration requires input/output hiding. Before export, the client removes inputs, outputs, serialized objects, attachments, and event content. Only selected metadata and numeric token usage are retained. Exception bodies are replaced with a generic error, and diagnostic log handlers withhold third-party exception content.

Request telemetry records route templates, status, method, timing, and generated request IDs. It does not record query values, headers, request bodies, filenames, or user-supplied path values. The container disables Uvicorn's raw access log and uses the structured request events instead.

These controls apply to application-generated telemetry. Managed platform request logs can still retain URLs and resource metadata; set their access and retention policies accordingly.

Review full evidence and recommendations inside the authenticated application. LangSmith production traces provide execution visibility without copying that content. The separate evaluation CLI uploads operator-supplied datasets only when explicitly invoked with `--langsmith`; keep evaluation data outside source control.

## Configure LangSmith

1. Create a private tracing project in the intended LangSmith workspace.
2. Create a workspace-appropriate API key with trace read/write and feedback-write access. Save it in a private local file, not in source code, an issue, a command argument, or Terraform variable values.
3. Create a Secret Manager secret and add the key as a version:

```bash
gcloud secrets create doci-langsmith-api-key \
  --project YOUR_PROJECT --replication-policy automatic

gcloud secrets versions add doci-langsmith-api-key \
  --project YOUR_PROJECT --data-file private-data/langsmith-api-key.txt
```

4. Configure the following values in your ignored Terraform variables file:

| Variable                      | Purpose                                                                   |
| ----------------------------- | ------------------------------------------------------------------------- |
| `enable_langsmith`            | Enable tracing and grant the application access to the referenced secret  |
| `langsmith_api_key_secret_id` | Secret ID, such as `doci-langsmith-api-key`; never the key value          |
| `langsmith_secret_version`    | Secret version to deploy; pin a numeric version for reproducibility       |
| `langsmith_project`           | Tracing project name; defaults to the application name plus `-production` |
| `langsmith_workspace_id`      | Workspace UUID, when required by the key                                  |
| `langsmith_endpoint`          | API region; defaults to `https://api.smith.langchain.com`                 |
| `langsmith_project_url`       | Private project URL used by the administrator's monitoring links          |
| `langsmith_sampling_rate`     | Fraction of reviews to trace, from `0` to `1`; defaults to `1`            |

Use `https://eu.api.smith.langchain.com` for an EU workspace. Sampling is deterministic by review ID so executions and approval resumptions of a selected review remain consistently sampled. Tracing does not bypass LangSmith's own workspace permissions.

Terraform stores the secret reference and IAM binding, not the LangSmith key value. Rotate the key by adding a Secret Manager version, updating the pinned version, and deploying a new revision. Remove or disable an obsolete key in LangSmith after verifying the replacement.

## Production dashboard and incidents

`enable_monitoring=true` provisions a dashboard, six log-based metrics, and operational alert policies. A custom-domain deployment also gets a multi-region HTTPS `/readyz` uptime check that validates a database connection.

The dashboard contains 14 charts for:

- HTTP request rate, server errors, and request latency.
- Review execution duration, execution/resumption counts, workflow failures, and decision submissions.
- Telemetry delivery failures.
- Pending tasks and dispatch delay.
- Cloud Run instances and Cloud SQL CPU, memory, and connections.

The alert policies cover:

| Signal                  | Default condition                                                                |
| ----------------------- | -------------------------------------------------------------------------------- |
| API server errors       | More than 5% 5xx for 5 minutes, with at least 5 errors in the aggregation window |
| Workflow failures       | A failed workflow execution in the 5-minute window                               |
| Trace delivery failures | A trace or feedback export failure in the 5-minute window                        |
| Queue dispatch failures | A saved review that could not be queued                                          |
| Slow reviews            | Highest revision-level p95 above 180 seconds for 5 minutes                       |
| Queue backlog           | More than 20 pending tasks for 10 minutes                                        |
| Readiness unavailable   | At least two probe locations failing for 3 minutes                               |

`monitoring_notification_channels` defaults to an empty list. In that mode incidents remain visible in the console and no notification channel is created. Supply existing channel IDs only when outbound notifications are wanted.

Tune `review_latency_threshold_ms` and `queue_backlog_threshold` to the workload. Graph execution counts include retries and approval resumptions; they are not unique case counts. Decision-submission counts can include idempotent retries. Native HTTP latency includes long-running worker requests. The p95 views use the maximum revision-level p95 rather than a blended global percentile.

New log-based metrics begin collecting after creation and can take a few minutes to appear. They do not backfill earlier application activity. Low-traffic charts can legitimately have gaps.

## Review quality feedback

The application attaches numeric feedback to sampled execution traces:

| Key                  | Meaning                                                         |
| -------------------- | --------------------------------------------------------------- |
| `citation_integrity` | Every finding cites an entry in the retrieved evidence snapshot |
| `reviewer_pass`      | The independent reviewer returned `pass`                        |
| `review_confidence`  | The model's confidence estimate, not a calibrated probability   |
| `human_approved`     | The persisted human decision on a resumed review was approval   |

The categorical `workflow_outcome` feedback records whether the execution paused for approval, resolved, was rejected, needs information, or escalated. Feedback delivery uses a bounded worker queue and a limited wait. Trace writes are flushed and trace visibility is checked before feedback is submitted with its canonical project and start time.

These are operational checks, not proof that a recommendation is factually correct. No LLM-as-judge evaluator is automatically enabled. Feedback does not intentionally extend trace retention; retention and limits remain controlled by the LangSmith workspace plan.

## Delivery and failure behavior

- LangSmith callbacks use the configured private client and flush within a bounded timeout after a graph invocation.
- Sampled Cloud Trace spans are ended and flushed before the final response byte, so request-based Cloud Run CPU allocation does not immediately suspend pending export work.
- Export and feedback errors produce `telemetry.delivery_failed` events. They do not authorize actions or change a successful workflow into a failed review.
- `cloud_trace_sampling_rate` defaults to `0.1`. Structured workflow events remain available even when an HTTP trace is not sampled.
- Readiness checks depend on the application database, not on LangSmith availability. A tracing outage must not make the application unavailable.

## Troubleshooting

**No LangSmith traces:** Check the project, region, workspace ID, sampling rate, Secret Manager version, and application service-account access. Inspect `telemetry.delivery_failed` logs. Run a new review; an old completed review is not automatically exported.

**Case processing failed:** Open its trace if available, then filter Cloud Logging by `review_id`. Inspect the failure stage and provider status without copying document contents into support tickets. Resolve the dependency issue and use **Retry review**.

**Queue growing:** Check task attempts, worker authentication, concurrency, and Cloud Run capacity. Increase concurrency only after checking database and model limits.

**Readiness failing:** Check TLS/DNS, the serving revision, Cloud SQL, and the connection pool. Uptime probes use the public load balancer; the direct Cloud Run ingress boundary remains in place.

**Dashboard chart empty:** Confirm that the relevant event has occurred and allow for metric ingestion delay. Compare native service metrics with structured logs before concluding that an empty custom chart is a service failure.

**First deployment reports a missing metric:** Cloud Monitoring may not yet recognize a newly created log-based metric when Terraform creates its alert. Keep the successfully provisioned resources, allow a few minutes for registration, then create and review a fresh Terraform plan before applying the remaining changes.

## References

- [LangSmith tracing quickstart](https://docs.langchain.com/langsmith/observability-quickstart)
- [Trace privacy controls](https://docs.langchain.com/langsmith/mask-inputs-outputs)
- [Trace sampling](https://docs.langchain.com/langsmith/sample-traces)
