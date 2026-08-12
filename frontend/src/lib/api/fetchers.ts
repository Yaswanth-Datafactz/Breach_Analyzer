// One typed function per endpoint the pages consume (docs/plan.md §5's API
// surface). Everything goes through the shared apiFetch (X-API-Key, error
// envelope -> ApiError); file exports use an authed blob download because
// the browser cannot attach the API key to a plain <a href>.

import { API_BASE_URL, ApiError, apiFetch, authHeaders, parseErrorBody } from './client'
import type {
  AccuracyRun,
  AgentRun,
  AgentRunDetail,
  Approval,
  ApprovalDecisionOut,
  CostSummary,
  DocumentOut,
  ExposurePage,
  PageOut,
  Passage,
  PersonDetail,
  Quarantine,
  ReviewDecision,
  ReviewDecisionOut,
  ReviewItem,
  Run,
} from './types'

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  }
  const qs = search.toString()
  return qs ? `?${qs}` : ''
}

// --- runs --------------------------------------------------------------------

export function listRuns(limit = 50, signal?: AbortSignal): Promise<PageOut<Run>> {
  return apiFetch(`/runs${query({ limit })}`, { signal })
}

export function getRun(runId: string, signal?: AbortSignal): Promise<Run> {
  return apiFetch(`/runs/${runId}`, { signal })
}

export function listRunQuarantines(
  runId: string,
  signal?: AbortSignal,
): Promise<PageOut<Quarantine>> {
  return apiFetch(`/runs/${runId}/quarantines${query({ limit: 100 })}`, { signal })
}

// --- documents -----------------------------------------------------------------

export function listDocuments(
  params: { class?: string; status?: string; limit?: number; offset?: number },
  signal?: AbortSignal,
): Promise<PageOut<DocumentOut>> {
  return apiFetch(`/documents${query(params)}`, { signal })
}

export function getPassage(passageId: string, signal?: AbortSignal): Promise<Passage> {
  return apiFetch(`/passages/${passageId}`, { signal })
}

// --- exposure & persons ----------------------------------------------------------

export interface ExposureQuery {
  search?: string
  category?: string
  review_status?: string
  min_confidence?: number
  limit?: number
  offset?: number
}

export function getExposure(params: ExposureQuery, signal?: AbortSignal): Promise<ExposurePage> {
  return apiFetch(`/exposure${query({ ...params })}`, { signal })
}

export function getPerson(personId: string, signal?: AbortSignal): Promise<PersonDetail> {
  return apiFetch(`/persons/${personId}`, { signal })
}

// --- review ------------------------------------------------------------------------

export function listReviewItems(
  params: { kind?: string; status?: string; limit?: number; offset?: number },
  signal?: AbortSignal,
): Promise<PageOut<ReviewItem>> {
  return apiFetch(`/review/items${query(params)}`, { signal })
}

export function decideReviewItem(
  itemId: string,
  body: {
    decision: ReviewDecision
    reviewer: string
    notes?: string
    after?: Record<string, unknown>
  },
): Promise<ReviewDecisionOut> {
  return apiFetch(`/review/items/${itemId}/decision`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

// --- agents -----------------------------------------------------------------------

export function listAgentRuns(
  params: { kind?: string; status?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<PageOut<AgentRun>> {
  return apiFetch(`/agents/runs${query(params)}`, { signal })
}

export function getAgentRun(agentRunId: string, signal?: AbortSignal): Promise<AgentRunDetail> {
  return apiFetch(`/agents/runs/${agentRunId}`, { signal })
}

export function listApprovals(
  status?: string,
  signal?: AbortSignal,
): Promise<PageOut<Approval>> {
  return apiFetch(`/agents/approvals${query({ status })}`, { signal })
}

export function decideApproval(
  approvalId: string,
  body: { decision: 'approved' | 'rejected'; decided_by: string; note?: string },
): Promise<ApprovalDecisionOut> {
  return apiFetch(`/agents/approvals/${approvalId}/decision`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

// --- costs ------------------------------------------------------------------------

/** 422 = "run has no cost events yet" -- a real, expected state before any
 * LLM tier has run (tier 0 is free by design). Surfaced as null so the
 * dashboard renders its branded empty card instead of an error. */
export async function getCostSummary(
  runId: string,
  signal?: AbortSignal,
): Promise<CostSummary | null> {
  try {
    return await apiFetch<CostSummary>(`/costs/summary${query({ run_id: runId })}`, { signal })
  } catch (err) {
    if (err instanceof ApiError && err.status === 422) return null
    throw err
  }
}

// --- accuracy -----------------------------------------------------------------------

let accuracyRoutePromise: Promise<boolean> | null = null

/** The accuracy router is mounted empty until the eval phase lands. Probe
 * the (auth-free, always-200) OpenAPI document for the route instead of
 * firing a request Chrome would log as a 404 console error: the moment the
 * endpoint ships, the path appears and the real GET below starts firing. */
function accuracyRouteExists(signal?: AbortSignal): Promise<boolean> {
  accuracyRoutePromise ??= (async () => {
    try {
      const origin = new URL(API_BASE_URL).origin
      const response = await fetch(`${origin}/openapi.json`, { signal })
      if (!response.ok) return false
      const spec = (await response.json()) as { paths?: Record<string, Record<string, unknown>> }
      return Boolean(spec.paths?.['/api/v1/accuracy/runs']?.get)
    } catch {
      accuracyRoutePromise = null // do not cache a transient failure
      return false
    }
  })()
  return accuracyRoutePromise
}

/** null = "no accuracy evaluation yet" (endpoint not shipped, or no runs). */
export async function listAccuracyRuns(signal?: AbortSignal): Promise<AccuracyRun[] | null> {
  if (!(await accuracyRouteExists(signal))) return null
  try {
    const page = await apiFetch<PageOut<AccuracyRun>>('/accuracy/runs', { signal })
    return page.items
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 405)) return null
    throw err
  }
}

// --- authed file downloads -----------------------------------------------------------

async function fetchBlob(path: string): Promise<Blob> {
  const response = await fetch(`${API_BASE_URL}${path}`, { headers: authHeaders() })
  if (!response.ok) {
    const { type, message } = await parseErrorBody(response)
    throw new ApiError(message, response.status, type)
  }
  return response.blob()
}

/** Browser-side authed download: the X-API-Key header cannot ride on a plain
 * <a href>, so fetch the bytes and hand them to the browser as a blob. */
export async function downloadAuthed(path: string, filename: string): Promise<void> {
  const blob = await fetchBlob(path)
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

export function exposureCsvPath(runId?: string): string {
  return `/exports/exposure.csv${query({ run_id: runId })}`
}

export function exposureXlsxPath(runId?: string): string {
  return `/exports/exposure.xlsx${query({ run_id: runId })}`
}

export function documentFilePath(documentId: string): string {
  return `/documents/${documentId}/file`
}
