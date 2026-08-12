import { Check, GitMerge, Split, UserRound } from 'lucide-react'
import { Card } from '../ui/Card'
import { Button } from '../ui/Button'
import { Pill } from '../ui/Pill'
import { Spinner } from '../ui/Spinner'
import { ConfidenceMeter } from '../confidence/ConfidenceMeter'
import { labelize, formatDate } from '../../lib/format'
import type { ReviewItem, ReviewMentionCard } from '../../lib/api/types'

function MentionColumn({ mention, side }: { mention: ReviewMentionCard | null; side: string }) {
  if (!mention) {
    return (
      <div className="flex-1 rounded-card border border-line bg-surface px-4 py-3 text-sm text-ink-muted">
        The {side} mention no longer exists.
      </div>
    )
  }
  return (
    <div className="min-w-0 flex-1 rounded-card border border-line bg-surface px-4 py-3">
      <div className="flex items-center gap-2">
        <UserRound className="h-4 w-4 shrink-0 text-ink-muted" aria-hidden="true" />
        <span className="truncate text-sm font-semibold text-ink">{mention.name_raw}</span>
      </div>
      <p className="mt-1 truncate text-xs text-ink-muted">
        {mention.document_filename ?? 'document'}
        {mention.dob ? ` · DOB ${formatDate(mention.dob)}` : ''}
      </p>
      {mention.elements.length === 0 ? (
        <p className="mt-2 text-xs text-ink-muted">No attached elements.</p>
      ) : (
        <ul className="mt-2 flex flex-col gap-1">
          {mention.elements.map((element) => (
            <li
              key={element.pii_element_id}
              className="flex items-center gap-2 rounded-btn border border-line bg-surface-raised px-2 py-1"
            >
              <span className="text-xs font-medium text-ink">{labelize(element.element_type)}</span>
              <code className="min-w-0 flex-1 truncate text-xs text-ink-muted">
                {element.value_raw}
              </code>
              {element.validation_status !== 'valid' ? (
                <Pill tone="danger" className="shrink-0">
                  {labelize(element.validation_status)}
                </Pill>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export type ErDecision = 'merge' | 'keep_separate'

/** docs/plan.md §7: the ER-pair queue renders both mentions' evidence sets
 * side by side; merge / keep-separate post real decisions that project into
 * identity_links (append-only) and recompute exposure. */
export function ErPairCard({
  item,
  busy,
  onDecide,
}: {
  item: ReviewItem
  busy: boolean
  onDecide: (item: ReviewItem, decision: ErDecision) => void
}) {
  const decided = item.status !== 'open'
  const score = typeof item.resolved.score === 'number' ? item.resolved.score : null
  return (
    <Card hoverLift={false} className="flex flex-col gap-3 px-4 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-sm font-semibold text-ink">Gray-band identity pair</span>
        <div className="flex items-center gap-2">
          <span className="text-xs text-ink-muted">pair score</span>
          <ConfidenceMeter confidence={score} />
        </div>
        {item.reason ? <Pill tone="brand">{labelize(item.reason)}</Pill> : null}
        <span className="ml-auto text-xs text-ink-muted">priority {item.priority}</span>
      </div>

      <div className="flex flex-col gap-3 lg:flex-row">
        <MentionColumn mention={item.resolved.left ?? null} side="left" />
        <MentionColumn mention={item.resolved.right ?? null} side="right" />
      </div>

      {decided ? (
        <div className="flex items-center gap-2 border-t border-line pt-2 text-sm text-ink-muted">
          <Check className="h-4 w-4" aria-hidden="true" />
          Decision recorded — this pair is {item.status}.
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-2">
          <Button type="button" size="sm" disabled={busy} onClick={() => onDecide(item, 'merge')}>
            {busy ? <Spinner className="h-3.5 w-3.5" /> : <GitMerge className="h-3.5 w-3.5" aria-hidden="true" />}
            Merge — same person
          </Button>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={() => onDecide(item, 'keep_separate')}
          >
            <Split className="h-3.5 w-3.5" aria-hidden="true" />
            Keep separate
          </Button>
        </div>
      )}
    </Card>
  )
}
