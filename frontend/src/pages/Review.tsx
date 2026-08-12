import { useState } from 'react'
import { ClipboardCheck, GitCompareArrows, ScanSearch } from 'lucide-react'
import { Card } from '../components/ui/Card'
import { ErrorBanner } from '../components/ui/ErrorBanner'
import { Spinner } from '../components/ui/Spinner'
import { EmptyState } from '../components/common/EmptyState'
import {
  ExtractionItemCard,
  type ExtractionDecision,
} from '../components/review/ExtractionItemCard'
import { ErPairCard, type ErDecision } from '../components/review/ErPairCard'
import { useAsync } from '../lib/useAsync'
import { decideReviewItem, listReviewItems } from '../lib/api/fetchers'
import { cn } from '../lib/cn'
import { formatInt } from '../lib/format'
import type { PageOut, ReviewItem } from '../lib/api/types'

type Tab = 'extraction' | 'er_pair'
type StatusFilter = 'open' | 'decided'

const REVIEWER = 'demo-reviewer'

/** docs/plan.md §7's Review page: the extraction queue and the ER-pair
 * queue. Decisions post to the real endpoint with an optimistic local flip
 * (the card shows "decision recorded" immediately) and roll back with an
 * error banner if the server refuses. */
export function ReviewPage() {
  const [tab, setTab] = useState<Tab>('extraction')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('open')
  const [busyItemId, setBusyItemId] = useState<string | null>(null)
  const [decisionError, setDecisionError] = useState<string | null>(null)

  const { data, loading, error, reload, mutate } = useAsync(
    (signal) => listReviewItems({ kind: tab, status: statusFilter, limit: 50 }, signal),
    [tab, statusFilter],
  )

  function patchItem(itemId: string, patch: Partial<ReviewItem>) {
    mutate((current: PageOut<ReviewItem>) => ({
      ...current,
      items: current.items.map((item) => (item.id === itemId ? { ...item, ...patch } : item)),
    }))
  }

  async function decide(
    item: ReviewItem,
    body: { decision: ExtractionDecision['decision'] | ErDecision; after?: Record<string, unknown> },
  ) {
    setBusyItemId(item.id)
    setDecisionError(null)
    const before = item.status
    patchItem(item.id, { status: 'decided' }) // optimistic
    try {
      await decideReviewItem(item.id, {
        decision: body.decision,
        reviewer: REVIEWER,
        after: body.after,
      })
    } catch (err) {
      patchItem(item.id, { status: before }) // rollback
      setDecisionError(err instanceof Error ? err.message : 'Decision failed')
    } finally {
      setBusyItemId(null)
    }
  }

  const tabs: Array<{ id: Tab; label: string; icon: typeof ScanSearch }> = [
    { id: 'extraction', label: 'Extraction queue', icon: ScanSearch },
    { id: 'er_pair', label: 'ER pairs', icon: GitCompareArrows },
  ]

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex rounded-btn border border-line bg-surface-raised p-0.5">
            {tabs.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                type="button"
                onClick={() => setTab(id)}
                className={cn(
                  'flex items-center gap-1.5 rounded-sm px-3 py-1.5 text-sm font-medium transition-colors',
                  tab === id
                    ? 'bg-brand-orange/10 text-brand-orange'
                    : 'text-ink-muted hover:text-ink',
                )}
                aria-pressed={tab === id}
              >
                <Icon className="h-4 w-4" aria-hidden="true" />
                {label}
              </button>
            ))}
          </div>
          <div className="ml-auto flex rounded-btn border border-line bg-surface-raised p-0.5">
            {(['open', 'decided'] as const).map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setStatusFilter(value)}
                className={cn(
                  'rounded-sm px-3 py-1.5 text-sm font-medium transition-colors',
                  statusFilter === value
                    ? 'bg-brand-orange/10 text-brand-orange'
                    : 'text-ink-muted hover:text-ink',
                )}
                aria-pressed={statusFilter === value}
              >
                {value === 'open' ? 'Open' : 'Decided'}
              </button>
            ))}
          </div>
        </div>

        {decisionError ? (
          <ErrorBanner message={`The decision was not applied: ${decisionError}`} />
        ) : null}

        {loading ? (
          <div className="flex justify-center py-16">
            <Spinner className="h-7 w-7" />
          </div>
        ) : error ? (
          <ErrorBanner message={error.message} onRetry={reload} />
        ) : data && data.items.length === 0 ? (
          <Card hoverLift={false}>
            <EmptyState
              icon={ClipboardCheck}
              title={statusFilter === 'open' ? 'Queue clear' : 'No decided items yet'}
              body={
                statusFilter === 'open'
                  ? tab === 'extraction'
                    ? 'No extraction findings await review. Items queue here when an element cannot be attributed to a person or falls below the confidence threshold.'
                    : 'No identity pairs await review. Gray-band pairs queue here for your teams or the adjudicator agent.'
                  : 'Decisions your teams record will appear here with their outcome.'
              }
            />
          </Card>
        ) : data ? (
          <>
            <p className="text-xs text-ink-muted">
              {formatInt(data.total)} {statusFilter} {tab === 'extraction' ? 'extraction items' : 'identity pairs'}
              {data.total > data.items.length
                ? ` · showing the first ${formatInt(data.items.length)}`
                : ''}
            </p>
            <div className="flex flex-col gap-3">
              {data.items.map((item) =>
                item.kind === 'extraction' ? (
                  <ExtractionItemCard
                    key={item.id}
                    item={item}
                    busy={busyItemId === item.id}
                    onDecide={(target, decision) =>
                      decide(target, {
                        decision: decision.decision,
                        after: 'after' in decision ? decision.after : undefined,
                      })
                    }
                  />
                ) : (
                  <ErPairCard
                    key={item.id}
                    item={item}
                    busy={busyItemId === item.id}
                    onDecide={(target, decision) => decide(target, { decision })}
                  />
                ),
              )}
            </div>
          </>
        ) : null}
      </div>
    </div>
  )
}
