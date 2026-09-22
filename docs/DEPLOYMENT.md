# Deploy to GCP

These instructions deploy Doci into your own Google Cloud project. You need active GCP billing, permissions to provision the services, and access to the configured Gemini models. Applying Terraform creates billable resources, including Cloud SQL.

## 1. Configure state and supporting infrastructure

Use an access-controlled GCS backend for shared Terraform state. Database connection strings and any LangSmith secret version are sensitive Terraform values and are present in state; do not commit state or `terraform.tfvars`.

```bash
gcloud auth login
gcloud auth application-default login
gcloud services enable cloudresourcemanager.googleapis.com --project YOUR_PROJECT
gcloud storage buckets create gs://YOUR_PROJECT-doci-tfstate --project YOUR_PROJECT \
  --location us-central1 --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://YOUR_PROJECT-doci-tfstate --versioning
cp infra/terraform.tfvars.example infra/terraform.tfvars
# Edit project_id and the desired region. Keep deploy_app=false initially.
terraform -chdir=infra init -backend-config=bucket=YOUR_PROJECT-doci-tfstate -backend-config=prefix=doci
terraform -chdir=infra plan -out=bootstrap.tfplan
terraform -chdir=infra apply bootstrap.tfplan
```

The bootstrap creates the artifact repository, runtime/task/build identities, IAM grants, Cloud SQL, object bucket, secrets, task queue, OCR processor, and Identity Platform configuration. To use an existing Identity Platform config, import it or set `manage_identity_platform=false`.

Cloud SQL uses its authenticated proxy socket mounted in Cloud Run. The instance has no authorized client IP ranges; connections are encrypted. Database backups and point-in-time recovery are enabled. HA and deletion protection default to enabled.

## 2. Configure user sign-in

Terraform enables email/password sign-in and creates a browser API key restricted to the authentication APIs and configured website origins. The key identifies the project and is intentionally exposed to the frontend. Supply `firebase_api_key` only to override it with an existing key. Create application users through an authorized Identity Platform administration workflow. Set `firebase_auth_domain` if using a custom auth domain.

Use an operator credential with Firebase Authentication administration permissions to assign each user's trusted custom claims:

```bash
uv run python -m doci.admin --project YOUR_PROJECT --uid USER_UID \
  --tenant YOUR_TEAM --role analyst
uv run python -m doci.admin --project YOUR_PROJECT --uid DIFFERENT_USER_UID \
  --tenant YOUR_TEAM --role approver
```

Available roles are `submitter`, `analyst`, `reviewer`, `approver`, and `admin`. User registration alone grants no case access: a token must include valid `tenant_id` and `role` claims. A case's creator and requesting analyst cannot approve its proposal even if their role later changes.

## 3. Build the first image

After bootstrap, use the repository URL from `terraform output`:

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev
docker build --platform linux/amd64 -t us-central1-docker.pkg.dev/YOUR_PROJECT/doci/app:initial .
docker push us-central1-docker.pkg.dev/YOUR_PROJECT/doci/app:initial
```

Alternatively, use Cloud Build's build-only mode for the first image:

```bash
gcloud builds submit --project YOUR_PROJECT \
  --tag us-central1-docker.pkg.dev/YOUR_PROJECT/doci/app:initial .
```

Configure `container_image` to the resulting immutable digest when available, set `deploy_app=true`, and set `allow_public_app=true` if the browser should reach the Firebase login page directly. Public reachability does not bypass application authentication. Leaving it false keeps the service behind Cloud Run IAM for environments with a separate access layer.

```bash
terraform -chdir=infra plan -out=application.tfplan
terraform -chdir=infra apply application.tfplan
terraform -chdir=infra output application_url
```

The API and UI share a service/origin. No frontend secrets are embedded at build time; the public sign-in configuration is served by `/api/config`. Cloud Tasks uses the deterministic service URL and a dedicated OIDC service identity. The worker verifies both the audience and that exact identity.

The initial SQL schema and LangGraph checkpoint tables are bootstrapped on startup under advisory locks. Future structural changes need explicit versioned database migrations; `create_all` does not migrate existing columns.

Session advisory-lock connections use autocommit. Keeping an open transaction on the lock connection would block LangGraph's concurrent index migrations. Case updates and approval/action records use their own normal database transactions.

For a custom website, set `custom_domain` and optionally `include_www=true`. Terraform provisions the HTTPS load balancer and managed certificate. Use the exact `squarespace_dns_records` output to point the apex and `www` A records at its IP. Cloud Run ingress is restricted to the load balancer and internal service traffic when a custom domain is configured.

If certificate validation failed before DNS was configured, first verify all domain A/AAAA records and the HTTPS forwarding rule. Google retries provisioning automatically. To deliberately request fresh issuance after correcting the configuration, increment `tls_certificate_revision`; Terraform creates the replacement before switching the proxy and removing the previous certificate. Avoid repeatedly rotating certificates while issuance is pending.

## 4. Optional services

- **LangSmith:** set `enable_langsmith=true` and supply `langsmith_api_key_secret_id` plus its version, project, endpoint, and workspace configuration. Create the key version directly in Secret Manager; no key value enters Terraform inputs or state. See [observability setup](OBSERVABILITY.md). Configure any separate reviewer tracing independently if required.
- **Separate reviewer:** set `enable_a2a_reviewer=true`. Terraform creates a private Cloud Run service and gives only the API identity invocation rights. It uses the official A2A SDK. Do not grant public invocation to the reviewer.
- **Document AI:** the OCR processor defaults to the `us` processor location. Text PDFs do not need OCR. Scanned/mixed PDFs require OCR and are subject to the processor's synchronous page limits, which can be lower than the application's 100-page text-PDF limit.
- **Neo4j:** use an existing managed Neo4j database and inject its configuration through your secret-management process. The Terraform module does not provision Neo4j. Relationship indexing is optional and SQL retrieval works when it is unavailable.

Choose a current model supported by your project/region. `GEMINI_MODEL` defaults to `gemini-3.5-flash` with the global endpoint. If your data residency requirements prohibit global routing, select a supported regional model and set `gemini_location` accordingly. See Google's [model lifecycle](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions).

## 5. CI/CD and maintenance

GitHub Actions runs the backend tests against SQLite and a disposable pgvector service, the frontend build, and Terraform validation. It does not deploy or contain cloud credentials.

`cloudbuild.yaml` tests, builds, pushes, and updates an **existing** Cloud Run service. Submit it using the provisioned build identity:

```bash
gcloud builds submit --project YOUR_PROJECT --config cloudbuild.yaml \
  --service-account projects/YOUR_PROJECT/serviceAccounts/doci-build@YOUR_PROJECT.iam.gserviceaccount.com \
  --substitutions _REGION=us-central1,_SERVICE=doci,_REPOSITORY=doci .
```

The operator needs permission to act as that build identity. Keep the Terraform image variable synchronized with the deployed image to avoid a later apply reverting it. The optional reviewer and reindex job also need their image updated via Terraform when shipping changes to shared agent code.

Run reindexing explicitly after an embedding-model change:

```bash
gcloud run jobs execute doci-reindex --project YOUR_PROJECT --region us-central1 --wait
```

Reindexing replaces stored embeddings in batches. Keep the configured embedding model stable while it runs; use a maintenance window or a versioned index for online migrations.

Cloud Run emits structured logs and sampled OpenTelemetry traces. The production dashboard and incident policies cover application, workflow, queue, trace-delivery, and readiness signals. Notification channels are optional; leave the channel list empty for console-only monitoring. See [the observability guide](OBSERVABILITY.md). After deployment, perform a real-project smoke test covering sign-in, text and scanned uploads, model access, task delivery, restart recovery, and independent human approval.

## Private evaluation datasets

Workspaces start empty. Dataset files and document packs are not part of this repository. For an optional evaluation workspace, set `synthetic_workspace=true`, choose a `test_tenant` beginning with `test-`, and set `seed_dataset_uri` to a private `gs://` object accessible to the application service account. Execute the seed job explicitly only after preparing the file; provisioning the job does not import records.

Print the accepted schema without loading a dataset:

```bash
uv run python -m doci.seed_testing --schema
uv run python -m doci.evaluation --schema
```

The importer requires a versioned dataset identifier, a `synthetic` classification, policy records, and case records with document filename/content pairs. Repeated imports use stable identifiers and verify document checksums. Changing content requires a new dataset version and document filenames. Imports never manufacture human approvals. Keep the input file in ignored `private-data/` locally or in private Cloud Storage.
