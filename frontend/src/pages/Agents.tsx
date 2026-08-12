import { useNavigate } from 'react-router-dom'
import { Bot, Wrench } from 'lucide-react'
import { Card } from '../components/ui/Card'
import { ErrorBanner } from '../components/ui/ErrorBanner'
import { Spinner } from '../components/ui/Spinner'
import { Pill } from '../components/ui/Pill'
import { EmptyState } from '../components/common/EmptyState'
import { StatusPill } from '../components/common/StatusPill'
import { BudgetMeter } from '../components/agents/BudgetMeter'
import { useAsync } from '../lib/useAsync'
import { listAgentRuns, listApprovals } from '../lib/api/fetchers'
import { AGENT_ICONS } from '../components/agents/icons'
import { formatDateTime, formatInt, formatUsd } from '../lib/format'

/** docs/plan.md §7's Agents page: every budgeted agent run with its status,
 * budget meter, and cost; pending approvals surface at the top. Row click
 * opens the full trace. */
export function AgentsPage() {
  const navigate = useNavigate()
  const runsState = useAsync((signal) => listAgentRuns({ limit: 50 }, signal), [])
  const approvalsState = useAsync((signal) => listApprovals('pending', signal), [])

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-4">
        {approvalsState.data && approvalsState.data.items.length > 0 ? (
          <Card hoverLift={false} className="border-brand-yellow/40 bg-brand-yellow/10 px-4 py-3">
            <p className="text-sm text-ink">
              {formatInt(approvalsState.data.items.length)}{' '}
              {approvalsState.data.items.length === 1 ? 'approval awaits' : 'approvals await'} your
              teams — open the parked run to decide.
            </p>
          </Card>
        ) : null}

        {runsState.loading ? (
          <div className="flex justify-center py-16">
            <Spinner className="h-7 w-7" />
          </div>
        ) : runsState.error ? (
          <ErrorBanner message={runsState.error.message} onRetry={runsState.reload} />
        ) : runsState.data && runsState.data.items.length === 0 ? (
          <Card hoverLift={false}>
            <EmptyState
              icon={Bot}
              title="No agent runs recorded"
              body="Budgeted, fully traced agent activity appears here: the orchestrator's checkpoints, exception investigations, ER adjudications, and QA audits."
            />
          </Card>
        ) : runsState.data ? (
          <>
            <p className="text-xs text-ink-muted">
              {formatInt(runsState.data.total)} agent runs · every step and tool call is persisted
              before it executes
            </p>
            <div className="flex flex-col gap-3">
              {runsState.data.items.map((run) => {
                const Icon = AGENT_ICONS[run.agent_kind] ?? Wrench
                return (
                  <Card
                    key={run.id}
                    className="cursor-pointer p-4"
                    onClick={() => navigate(`/agents/runs/${run.id}`)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault()
                        navigate(`/agents/runs/${run.id}`)
                      }
                    }}
                  >
                    <div className="flex flex-wrap items-center gap-3">
                      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-btn bg-brand-orange/10">
                        <Icon className="h-4.5 w-4.5 text-brand-orange" aria-hidden="true" />
                      </span>
                      <div className="min-w-0">
                        <p className="text-sm font-semibold capitalize text-ink">{run.agent_kind}</p>
                        <p className="text-xs text-ink-muted">{formatDateTime(run.created_at)}</p>
                      </div>
                      <StatusPill status={run.status} />
                      {run.model ? <Pill tone="neutral">{run.model}</Pill> : null}
                      <span className="ml-auto text-sm tabular-nums text-ink">
                        {formatUsd(run.cost_usd)}
                      </span>
                    </div>
                    <div className="mt-3">
                      <BudgetMeter
                        compact
                        budget={{
                          max_steps: run.budget_max_steps,
                          max_tokens: run.budget_max_tokens,
                          max_usd: run.budget_max_usd,
                          steps_used: run.steps_used,
                          tokens_used: run.tokens_in + run.tokens_out,
                          cost_usd: run.cost_usd,
                        }}
                      />
                    </div>
                  </Card>
                )
              })}
            </div>
          </>
        ) : null}
      </div>
    </div>
  )
}
