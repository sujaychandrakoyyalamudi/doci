# Security

Doci handles document content, user identities, and case decisions. Keep those records and their credentials outside source control.

## Reporting an issue

Use GitHub's private vulnerability reporting option when available. If it is unavailable, open a public issue that requests a private contact channel without including exploit details, credentials, document content, or customer identifiers. Do not post live access tokens or database exports in issues or pull requests.

## Configuration boundaries

- Local development uses deterministic agents and a local role selector. Bind it to loopback and do not expose it as an authenticated service.
- Production requires Identity Platform authentication, trusted tenant/role claims, PostgreSQL, Cloud Storage, and authenticated Cloud Tasks delivery.
- The submitter and requesting analyst cannot approve their own proposal. A document, model response, or sponsor attestation cannot replace a persisted human decision.
- Treat retrieved documents and model output as untrusted content. Keep external actions behind explicit, action-specific controls.
- LangSmith tracing is opt-in and hides model inputs and outputs by default. Evaluation uploads are a separate explicit operation.

## Files to keep private

Store credentials in your secret manager or ignored local configuration. Keep case documents, evaluation datasets, application login files, private environment profiles, Terraform state and plans, database files, and generated document packs out of commits. The repository's `.gitignore` covers the project's standard locations, but ignore rules cannot detect every misplaced credential.

Review the staged file list and run a secret scanner before publishing. If a credential is committed, rotate it promptly and address repository history; deleting the current file alone does not remove earlier copies.
