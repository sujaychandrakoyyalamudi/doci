# Operational boundaries and recovery

This is a runnable initial implementation, not an assertion of production certification. GCP integrations require live-project acceptance testing, representative model evaluation, and your organization's operational controls.

## Recovery

- A failed run retains its LangGraph checkpoint. Use **Retry review** to continue that same run. A crash after recording an approved internal action cannot create a second action because the run identifier is unique in the action table.
- Cloud Tasks retries a failed worker response. The worker verifies its OIDC audience and service-account email. Simultaneous deliveries are serialized by a PostgreSQL advisory lock.
- Queue publication follows the database commit. If dispatch fails, the API reports a saved but undispatched run; retry that run. There is a crash window between commit and queue publication. Operate a queued-run reconciler or add a transactional outbox before requiring unattended delivery guarantees.
- Local background execution is not a durable task queue. A process restart preserves graph checkpoints but requires an explicit retry of queued/processing work.
- Rejected, escalated, or needs-information cases can receive additional documents and a fresh review. Evidence is locked during active reviews and after resolution. Failed runs must be retried before evidence is changed.

## Limits and scope

- Uploads: 15 MB, UTF-8 TXT/Markdown/CSV or valid PDF; up to 100 PDF pages and 1 million extracted characters. PDF extraction is synchronous. Document AI processor quotas/page limits also apply to scanned files.
- Demo retrieval scans at most 5,000 eligible chunks per collection. The workspace lists up to 500 cases and 1,000 documents, with the latest 100 audit events. Server pagination and larger-scale full-text/vector indexing are future work.
- PostgreSQL vector retrieval currently uses exact distance scans over tenant/case-filtered candidates. Benchmark and add suitable pgvector indexes before large deployments.
- Model confidence is not calibrated. Citation validation proves the reference exists, not that a claim is supported. An independent reviewer and human approval provide separate checks, but the UI must not be used to infer autonomous financial or legal authority.
- Approved actions are internal records. Integrate external systems through a durable action outbox with the destination's idempotency keys; the database uniqueness constraint alone cannot guarantee exactly-once external effects.
- Document and audit retention, legal holds, malware scanning, customer-managed encryption keys, tamper-evident exports, end-user quotas/rate limiting, data residency review, and domain-specific policy governance are not implemented.
- Tenant isolation is enforced at the application query layer, not PostgreSQL row-level security. Cloud runtime and database administrators are trusted operators.
- Policies are append-only documents in this initial version. There is no effective-date/version-retirement policy workflow. Establish one before real policy compliance use.
- A2A review is stateless and request/response only. MCP is a stdio adapter using the supplied user's API token; token refresh and hosted multi-user MCP sessions are outside this initial adapter.
- Neo4j stores explicit document relationships only. It does not derive or validate policy relationships from natural language.

## Handling document instructions

Retrieved text is always data. The analyst and reviewer prompts explicitly forbid obeying embedded commands, following document URLs, changing identity, approving actions, or revealing secrets. Tool access is not given to those chains. Citation IDs are checked against the snapshot before a proposal reaches human approval. The final action checks the persisted human decision, proposal hash, independent identity, current run, and reviewer verdict again.

The deterministic prompt-injection test verifies that document commands cannot bypass approval plumbing. It is not a model-security benchmark. Test live-model behavior against your own adversarial and representative documents before operational use.
