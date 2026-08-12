import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, CircleSlash, Info, Wrench } from 'lucide-react'
import { Card } from '../components/ui/Card'
import { Pill } from '../components/ui/Pill'
import { Spinner } from '../components/ui/Spinner'
import { ErrorBanner } from '../components/ui/ErrorBanner'
import { EmptyState } from '../components/common/EmptyState'
import { StatusPill } from '../components/common/StatusPill'
import { ApprovalBanner } from '../components/agents/ApprovalBanner'
import { BudgetMeter } from '../components/agents/BudgetMeter'
import { TraceTimeline } from '../components/agents/TraceTimeline'
import { AGENT_ICONS } from '../components/agents/icons'
import { useAsync } from '../lib/useAsync'
import { getAgentRun, listApprovals } from '../lib/api/fetchers'
import { formatDateTime, formatInt } from '../lib/format'

/** docs/plan.md §7: the full persisted trace of one agent run -- budget
 * meter, step timeline with expandable tool calls, the approval gate when
 * the run is parked, and a loud terminal banner when a budget was
 * exhausted. */
export function AgentRunDetailPage() {
  const { agentRunId } = useParams<{ agentRunId: string }>()
  const { data, loading, error, reload } = useAsync(
    (signal) => getAgentRun(agentRunId ?? '', signal),
    [agentRunId],
  )
  const approvalsState = useAsync((signal) => listApprovals(undefined, signal), [agentRunId])

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="h-7 w-7" />
      </div>
    )
  }
  if (error) {
    return (
      <div className="p-6">
        <ErrorBanner message={error.message} onRetry={reload} />
      </div>
    )
  }
  if (!data) return null

  const Icon = AGENT_ICONS[data.agent_kind] ?? Wrench
  const approvalsForRun = (approvalsState.data?.items ?? []).filter(
    (approval) => approval.agent_run_id === data.id,
  )
  const pendingApproval = approvalsForRun.find((approval) => approval.status === 'pending')
  const decidedApproval = approvalsForRun.find((approval) => approval.status !== 'pending')

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-4">
        <Link
          to="/agents"
          className="flex w-fit items-center gap-1.5 text-sm text-ink-muted transition-colors hover:text-brand-orange"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          Back to agent runs
        </Link>

        <Card hoverLift={false} className="p-5">
          <div className="flex flex-wrap items-center gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-btn bg-brand-orange/10">
              <Icon className="h-5 w-5 text-brand-orange" aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <h2 className="text-base font-semibold capitalize text-ink">{data.agent_kind}</h2>
              <p className="text-xs text-ink-muted">
                {formatDateTime(data.created_at)} · run {data.id.slice(0, 8)}
              </p>
            </div>
            <StatusPill status={data.status} />
            {data.model ? <Pill tone="neutral">{data.model}</Pill> : null}
          </div>
          <div className="mt-4">
            <BudgetMeter budget={data.budget} />
          </div>
          <div className="mt-4 rounded-btn border border-line bg-surface px-3 py-2">
            <p className="mb-1 text-xs font-medium uppercase tracking-wide text-ink-muted">
              Trigger
            </p>
            <pre className="overflow-x-auto text-xs leading-5 text-ink">
              {JSON.stringify(data.trigger, null, 2)}
            </pre>
          </div>
        </Card>

        {data.status === 'budget_exceeded' ? (
          <div className="flex items-start gap-3 rounded-card border border-brand-red/40 bg-brand-red/10 px-4 py-3">
            <CircleSlash className="mt-0.5 h-5 w-5 shrink-0 text-brand-red" aria-hidden="true" />
            <div>
              <p className="text-sm font-medium text-ink">
                Budget exhausted after {formatInt(data.steps_used)} of{' '}
                {formatInt(data.budget_max_steps)} steps
              </p>
              <p className="mt-0.5 text-xs leading-5 text-ink-muted">
                The runner parked this run with its partial findings preserved below — the trace
                shows exactly how far the diagnosis got before the cap.
              </p>
            </div>
          </div>
        ) : null}

        {pendingApproval ? (
          <ApprovalBanner
            approval={pendingApproval}
            onDecided={() => {
              reload()
              approvalsState.reload()
            }}
          />
        ) : null}
        {decidedApproval ? (
          <div className="flex items-start gap-3 rounded-card border border-line bg-surface-raised px-4 py-3">
            <Info className="mt-0.5 h-4 w-4 shrink-0 text-ink-muted" aria-hidden="true" />
            <p className="text-sm text-ink-muted">
              {decidedApproval.action_type.replaceAll('_', ' ')} was {decidedApproval.status} by{' '}
              {decidedApproval.decided_by ?? 'a reviewer'}
              {decidedApproval.decided_at ? ` on ${formatDateTime(decidedApproval.decided_at)}` : ''}
              .
            </p>
          </div>
        ) : null}

        {data.outcome ? (
          <Card hoverLift={false} className="p-4">
            <p className="mb-1 text-xs font-medium uppercase tracking-wide text-ink-muted">
              Outcome
            </p>
            <pre className="overflow-x-auto text-xs leading-5 text-ink">
              {JSON.stringify(data.outcome, null, 2)}
            </pre>
          </Card>
        ) : null}

        <Card hoverLift={false} className="p-5">
          <h3 className="mb-3 text-sm font-semibold text-ink">Trace</h3>
          {data.steps.length === 0 ? (
            <EmptyState
              icon={Info}
              title="No steps recorded"
              body="This run has not produced any persisted steps."
            />
          ) : (
            <TraceTimeline steps={data.steps} />
          )}
        </Card>
      </div>
    </div>
  )
}
