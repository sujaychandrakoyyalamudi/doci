export type Config = {
  auth_mode: "demo" | "firebase";
  model_provider: "demo" | "vertex";
  environment: string;
  version: string;
  firebase_api_key?: string;
  firebase_auth_domain?: string;
  project_id?: string;
  synthetic_workspace?: boolean;
};
export type Actor = {
  id: string;
  tenant_id: string;
  role: string;
  name: string;
};
export type Case = {
  id: string;
  reference: string;
  title: string;
  description: string;
  category: string;
  priority: string;
  status: string;
  created_by: string;
  assigned_to: string;
  current_run_id: string | null;
  created_at: string;
  updated_at: string;
  document_count: number;
};
export type Document = {
  id: string;
  case_id: string | null;
  filename: string;
  size: number;
  kind: string;
  content_type: string;
  pages: number;
  created_at: string;
};
export type Evidence = {
  chunk_id: string;
  document_id: string;
  filename: string;
  page: number;
  quote: string;
  kind: string;
  score: number;
};
export type Analysis = {
  summary: string;
  findings: { statement: string; citation_ids: string[] }[];
  action: string;
  action_title: string;
  rationale: string;
  confidence: number;
  missing_information: string[];
};
export type Run = {
  id: string;
  requested_by: string;
  status: string;
  step: string;
  mode: string;
  evidence: Evidence[];
  analysis: Partial<Analysis>;
  review: { verdict?: string; reasoning?: string; concerns?: string[] };
  confidence: number | null;
  revision: number;
  error: string | null;
  trace_id: string;
  proposal_hash: string;
};
export type Event = {
  id: string;
  case_id: string | null;
  actor: string;
  event: string;
  detail: string;
  created_at: string;
};
export type Detail = Case & {
  documents: Document[];
  run: Run | null;
  events: Event[];
  decision: { decision: string; comment: string; actor_id: string } | null;
  action: { kind: string; approved_by: string } | null;
};
