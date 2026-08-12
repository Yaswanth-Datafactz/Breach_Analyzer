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

export interface PageOut<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

// --- runs (schemas/run.py) --------------------------------------------------

export interface Run {
  id: string
  status: string
  config_snapshot: Record<string, unknown>
  counters: Record<string, number>
  started_at: string | null
  finished_at: string | null
  created_at: string
}

// --- documents & passages (schemas/document.py, schemas/passage.py) ---------

export interface DocumentOut {
  id: string
  run_id: string
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
  created_at: string
}

// --- quarantines (schemas/quarantine.py) ------------------------------------

export interface Quarantine {
  id: string
  document_id: string
  document_filename: string
  document_rel_path: string
  stage: string
  reason_code: string
  detail: string | null
  status: string
  created_at: string
}

// --- exposure & persons (schemas/exposure.py) --------------------------------

export interface PersonAlias {
  name: string
  kind: 'nickname' | 'maiden' | 'initials' | 'misspelling' | 'order_variant' | string
}

export interface FlagOut {
  id: string
  category: PiiCategory | string
  exposed: boolean
  confidence: number | null
  review_status: string
  evidence_count: number
}

export interface PersonSummary {
  id: string
  run_id: string
  best_name: string
  aliases: PersonAlias[]
  dob: string | null
  er_confidence: number | null
  review_status: string
  mention_count: number
  document_count: number
  flags: FlagOut[]
}

export interface ExposurePage extends PageOut<PersonSummary> {
  run_id: string
}

export interface EvidenceRef {
  id: string
  pii_element_id: string
  element_type: string
  document_id: string
  document_filename: string | null
  passage_id: string
  snippet: string | null
  char_start: number | null
  char_end: number | null
  detector: string | null
  validation_status: string | null
}

export interface FlagDetail extends FlagOut {
  evidence: EvidenceRef[]
}

export interface IdentityLink {
  id: string
  mention_id: string
  mention_name_raw: string | null
  document_filename: string | null
  score: number | null
  method: 'rule' | 'agent' | 'reviewer' | string
  rule_id: string | null
  rationale: string | null
  active: boolean
  created_at: string
}

export interface PersonDetail {
  id: string
  run_id: string
  best_name: string
  aliases: PersonAlias[]
  dob: string | null
  er_confidence: number | null
  review_status: string
  mention_count: number
  document_count: number
  flags: FlagDetail[]
  identity_links: IdentityLink[]
}

// --- review (schemas/review.py + services/review.py resolved payloads) -------

/** services/review.py `_element_card` -- the extraction queue's card. */
export interface ReviewElementCard {
  pii_element_id: string
  element_type: string
  value_raw: string
  validation_status: string
  detector: string | null
  confidence: number | null
  document_id: string
  document_filename: string | null
  passage_id: string
  char_start: number | null
  char_end: number | null
  snippet: string | null
  signals: Record<string, unknown> | null
}

/** services/review.py `_mention_card` -- one side of an ER pair. */
export interface ReviewMentionCard {
  mention_id: string
  name_raw: string
  dob: string | null
  document_id: string
  document_filename: string | null
  passage_id: string
  person_id: string | null
  features: Record<string, unknown> | null
  elements: Array<{
    pii_element_id: string
    element_type: string
    value_raw: string
    validation_status: string
    char_start: number | null
    char_end: number | null
  }>
}

export interface ReviewItem {
  id: string
  kind: 'extraction' | 'er_pair' | 'flag_audit'
  ref: Record<string, unknown>
  reason: string | null
  priority: number
  status: 'open' | 'decided' | 'dismissed' | string
  created_at: string
  resolved: {
    element?: ReviewElementCard | null
    score?: number | null
    left?: ReviewMentionCard | null
    right?: ReviewMentionCard | null
    ref?: Record<string, unknown>
  }
}

export type ReviewDecision =
  | 'correct'
  | 'attach'
  | 'dismiss'
  | 'merge'
  | 'keep_separate'
  | 'confirm'
  | 'override'

export interface ReviewDecisionOut {
  review_item_id: string
  decision: string
  status: string
  recomputed_person_ids: string[]
  detail: Record<string, unknown>
}

// --- agents (schemas/agent.py) -----------------------------------------------

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
  model: string | null
  status: AgentRunStatus
  budget_max_steps: number | null
  budget_max_tokens: number | null
  budget_max_usd: number | null
  steps_used: number
  tokens_in: number
  tokens_out: number
  cost_usd: number
  outcome: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface AgentToolCall {
  id: string
  tool_name: string
  args: Record<string, unknown>
  result_summary: Record<string, unknown> | null
  is_error: boolean
  latency_ms: number | null
  created_at: string
}

export interface AgentStep {
  id: string
  step_no: number
  request_summary: Record<string, unknown> | null
  response_summary: Record<string, unknown> | null
  stop_reason: string | null
  tokens_in: number | null
  tokens_out: number | null
  latency_ms: number | null
  cost_usd: number | null
  created_at: string
  tool_calls: AgentToolCall[]
}

export interface AgentBudget {
  max_steps: number | null
  max_tokens: number | null
  max_usd: number | null
  steps_used: number
  tokens_used: number
  cost_usd: number
}

export interface AgentRunDetail extends AgentRun {
  steps: AgentStep[]
  budget: AgentBudget
}

export interface Approval {
  id: string
  agent_run_id: string
  action_type: 'bulk_merge' | 'final_signoff' | 'class_pause' | string
  payload: Record<string, unknown>
  status: 'pending' | 'approved' | 'rejected' | string
  decided_by: string | null
  decided_at: string | null
  created_at: string
}

export interface ApprovalDecisionOut {
  approval: Approval
  run_status: string
  resumed: boolean
}

// --- costs (schemas/cost.py) --------------------------------------------------

export interface CostSummaryRow {
  purpose: string
  tier: string
  model: string
  calls: number
  input_tokens: number
  output_tokens: number
  cached_input_tokens: number
  cache_hit_rate: number
  cost_usd: number
}

export interface CostTotals {
  calls: number
  input_tokens: number
  output_tokens: number
  cached_input_tokens: number
  cache_hit_rate: number
  cost_usd: number
}

export interface CostSummary {
  run_id: string
  rows: CostSummaryRow[]
  totals: CostTotals
}

// --- accuracy (router mounted empty until the eval phase lands) ---------------

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
