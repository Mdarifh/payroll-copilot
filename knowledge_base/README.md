# Payroll knowledge base

Documents the policy assistant (RAG) may answer from. Every document needs a
sidecar `<file>.meta.yaml` with: module, document_name, rule_name, version,
effective_from, effective_to, jurisdiction (`IN` or `IN-<STATE>`), source,
status, doc_type. Documents are stored under the folder of their module.

- `*/generated/` - **generated** from `config/rules/**` by `payroll-copilot kb generate`.
  Do not edit; change the rule YAML and regenerate.
- Other files - demo company policies describing the conventions this system is
  configured with (fictional company). They are not statements of law.

Supported formats: Markdown, TXT, PDF, DOCX. Ingest with `payroll-copilot kb ingest`.
