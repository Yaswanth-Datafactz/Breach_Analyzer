// Mirrors backend/app/schemas/*.py -- kept as plain interfaces (no runtime
// validation) since this is an internal tool talking to a service we also
// own; the backend's own Pydantic models are the actual contract enforcement.
// Enum-like unions mirror docs/plan.md §4's column enums.

export type FileClass =
  | 'pdf_digital'
  | 'pdf_scanned'
  | 'docx'
  | 'xlsx'
  | 'csv'
  | 'eml'
  | 'msg'
  | 'html'
  | 'txt'
  | 'png'
  | 'unknown'

export type DocumentStatus = 'queued' | 'parsed' | 'extracted' | 'quarantined' | 'failed' | 'done'

export type PiiCategory =
  | 'ssn'
  | 'dob'
  | 'drivers_license'
  | 'passport'
  | 'financial_account'
  | 'credit_card'
  | 'medical'
  | 'credentials'
  | 'address'
  | 'phone'
  | 'email'

export type FlagReviewStatus = 'auto' | 'human_confirmed' | 'human_overridden'

export interface PageOut<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface Run {
  id: string
  status: string
  config_snapshot: Record<string, unknown>
  counters: Record<string, number>
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface DocumentSummary {
  id: string
  sha256: string
  original_filename: string
  rel_path: string
  declared_mime: string | null
  sniffed_mime: string | null
  byte_size: number
  file_class: FileClass
  source_kind: 'corpus' | 'attachment'
  parent_document_id: string | null
  status: DocumentStatus
  page_count: number | null
  is_image_based: boolean | null
  created_at: string
}

export interface Passage {
  id: string
  document_id: string
  seq: number
  kind: 'page' | 'sheet_range' | 'email_part' | 'text_block'
  locator: Record<string, unknown>
  text: string
  ocr: boolean
  page_image_sha: string | null
}

export interface PersonAlias {
  name: string
  kind: 'nickname' | 'maiden' | 'initials' | 'misspelling' | 'order_variant'
}

export interface ExposureFlag {
  category: PiiCategory
  exposed: boolean
  confidence: number | null
  review_status: FlagReviewStatus
  evidence_count: number
}

export interface ExposureRow {
  person_id: string
  best_name: string
  aliases: PersonAlias[]
  dob: string | null
  flags: ExposureFlag[]
  document_count: number
  mention_count: number
  er_confidence: number | null
  review_status: string
}

export interface FlagEvidence {
  pii_element_id: string
  document_id: string
  passage_id: string
  snippet: string
}

export interface IdentityLink {
  mention_id: string
  score: number | null
  method: 'rule' | 'agent' | 'reviewer'
  rule_id: string | null
  rationale: string | null
  agent_run_id: string | null
  active: boolean
}

export interface PersonDetail extends ExposureRow {
  evidence: Record<string, FlagEvidence[]>
  links: IdentityLink[]
}

export interface ReviewItem {
  id: string
  kind: 'extraction' | 'er_pair' | 'flag_audit'
  ref: Record<string, unknown>
  reason: string
  priority: number
  status: string
  created_at: string
}

export type AgentKind = 'orchestrator' | 'investigator' | 'adjudicator' | 'auditor'

export type AgentRunStatus =
  | 'running'
  | 'succeeded'
  | 'escalated'
  | 'budget_exceeded'
  | 'awaiting_approval'
  | 'failed'

export interface AgentRun {
  id: string
  agent_kind: AgentKind
  trigger: Record<string, unknown>
  model: string
  status: AgentRunStatus
  budget_max_steps: number
  budget_max_tokens: number | null
  budget_max_usd: number
  steps_used: number
  tokens_in: number
  tokens_out: number
  cost_usd: number
  outcome: Record<string, unknown> | null
  created_at: string
}

export interface AgentToolCall {
  tool_name: string
  args: Record<string, unknown>
  result_summary: Record<string, unknown> | null
  is_error: boolean
  latency_ms: number | null
}

export interface AgentStep {
  id: string
  agent_run_id: string
  step_no: number
  request_summary: Record<string, unknown> | null
  response_summary: Record<string, unknown> | null
  stop_reason: string | null
  tokens: number | null
  latency_ms: number | null
  cost_usd: number | null
  tool_calls: AgentToolCall[]
}

export interface CostBreakdownRow {
  purpose: string
  model: string
  calls: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  cost_usd: number
}

export interface CostSummary {
  run_id: string
  total_cost_usd: number
  cost_per_document_usd: number | null
  by_purpose: CostBreakdownRow[]
}

export interface AccuracyRun {
  id: string
  config_snapshot: Record<string, unknown>
  metrics: Record<string, unknown>
  created_at: string
}

// Signal-provenance shape consumed by the shared MethodBadge (byte-identical
// UC2 copy -- docs/plan.md D7). UC3 maps pii_elements.detector/signals into
// this surface wherever the review UI renders a provenance badge.
export interface FieldOut {
  presence: 'extracted' | 'absent_confirmed' | 'extraction_failed'
  corrected: boolean
  signal_grounding: number | null
  signal_logprob: number | null
  signal_critic: number | null
  signal_validator: number | null
  signal_agreement: number | null
}
