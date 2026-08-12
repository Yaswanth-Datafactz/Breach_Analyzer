import { useState } from 'react'
import { ShieldQuestion } from 'lucide-react'
import { Button } from '../ui/Button'
import { ErrorBanner } from '../ui/ErrorBanner'
import { Spinner } from '../ui/Spinner'
import { decideApproval } from '../../lib/api/fetchers'
import type { Approval, ApprovalDecisionOut } from '../../lib/api/types'

/** The human gate (docs/plan.md §3): a parked run resumes only on a human
 * decision. Approve/reject post to the real endpoint; the caller refreshes
 * the run from the response. */
export function ApprovalBanner({
  approval,
  onDecided,
}: {
  approval: Approval
  onDecided: (result: ApprovalDecisionOut) => void
}) {
  const [busy, setBusy] = useState<'approved' | 'rejected' | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function decide(decision: 'approved' | 'rejected') {
    setBusy(decision)
    setError(null)
    try {
      const result = await decideApproval(approval.id, {
        decision,
        decided_by: 'demo-reviewer',
      })
      onDecided(result)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Decision failed')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="rounded-card border border-brand-yellow/40 bg-brand-yellow/10 px-4 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <ShieldQuestion className="h-5 w-5 shrink-0 text-brand-yellow" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-ink">
            Awaiting approval: {approval.action_type.replaceAll('_', ' ')}
          </p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {typeof approval.payload.rationale === 'string'
              ? approval.payload.rationale
              : 'The agent parked this run until your teams decide.'}
            {typeof approval.payload.cluster_size === 'number'
              ? ` Cluster impact: ${approval.payload.cluster_size} persons.`
              : ''}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            type="button"
            size="sm"
            onClick={() => decide('approved')}
            disabled={busy !== null}
          >
            {busy === 'approved' ? <Spinner className="h-3.5 w-3.5" /> : null}
            Approve
          </Button>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => decide('rejected')}
            disabled={busy !== null}
          >
            {busy === 'rejected' ? <Spinner className="h-3.5 w-3.5" /> : null}
            Reject
          </Button>
        </div>
      </div>
      {error ? <ErrorBanner message={error} className="mt-2" /> : null}
    </div>
  )
}
