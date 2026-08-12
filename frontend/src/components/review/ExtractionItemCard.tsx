import { useState } from 'react'
import { Check, FileText, Undo2, X } from 'lucide-react'
import { Button } from '../ui/Button'
import { Card } from '../ui/Card'
import { Pill } from '../ui/Pill'
import { Spinner } from '../ui/Spinner'
import { ConfidenceMeter } from '../confidence/ConfidenceMeter'
import { labelize, formatDateTime } from '../../lib/format'
import type { ReviewItem } from '../../lib/api/types'

const ELEMENT_TYPES = [
  'ssn',
  'ssn_last4',
  'dob',
  'drivers_license',
  'passport',
  'financial_account',
  'credit_card',
  'medical',
  'credential',
  'address',
  'phone',
  'email',
  'name',
]

export type ExtractionDecision =
  | { decision: 'correct'; after: Record<string, unknown> }
  | { decision: 'dismiss' }

/** One extraction-queue item: the proposed element with its passage snippet
 * and provenance, and the three reviewer actions -- accept (confirm valid),
 * reclassify (correct the element type), reject (downgrade to a trap).
 * All three post real review decisions (services/review.py's vocabulary:
 * correct / dismiss). */
export function ExtractionItemCard({
  item,
  busy,
  onDecide,
}: {
  item: ReviewItem
  busy: boolean
  onDecide: (item: ReviewItem, decision: ExtractionDecision) => void
}) {
  const [reclassifyTo, setReclassifyTo] = useState('')
  const element = item.resolved.element
  const decided = item.status !== 'open'

  if (!element) {
    return (
      <Card hoverLift={false} className="px-4 py-3 text-sm text-ink-muted">
        The element behind this item no longer exists. Reason: {item.reason ?? 'unknown'}.
      </Card>
    )
  }

  return (
    <Card hoverLift={false} className="flex flex-col gap-3 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-ink">{labelize(element.element_type)}</span>
        <code className="rounded-btn border border-line bg-surface px-2 py-0.5 text-xs text-ink">
          {element.value_raw}
        </code>
        {element.detector ? <Pill tone="neutral">{element.detector}</Pill> : null}
        <Pill tone={element.validation_status === 'valid' ? 'neutral' : 'danger'}>
          {labelize(element.validation_status)}
        </Pill>
        {item.reason ? <Pill tone="brand">{labelize(item.reason)}</Pill> : null}
        <span className="ml-auto text-xs text-ink-muted">{formatDateTime(item.created_at)}</span>
      </div>

      <div className="flex items-start gap-2 text-xs text-ink-muted">
        <FileText className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <div className="min-w-0">
          <p className="truncate">{element.document_filename ?? 'document'}</p>
          {element.snippet ? (
            <p className="mt-1 line-clamp-2 leading-5">…{element.snippet}…</p>
          ) : null}
        </div>
        {element.confidence !== null ? (
          <ConfidenceMeter confidence={element.confidence} className="ml-auto shrink-0" />
        ) : null}
      </div>

      {decided ? (
        <div className="flex items-center gap-2 border-t border-line pt-2 text-sm text-ink-muted">
          <Check className="h-4 w-4" aria-hidden="true" />
          Decision recorded — this item is {item.status}.
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-2">
          <Button
            type="button"
            size="sm"
            disabled={busy}
            onClick={() => onDecide(item, { decision: 'correct', after: { validation_status: 'valid' } })}
          >
            {busy ? <Spinner className="h-3.5 w-3.5" /> : <Check className="h-3.5 w-3.5" aria-hidden="true" />}
            Accept as valid
          </Button>
          <div className="flex items-center gap-1">
            <select
              value={reclassifyTo}
              onChange={(event) => setReclassifyTo(event.target.value)}
              disabled={busy}
              aria-label="Reclassify element type"
              className="h-8 rounded-btn border border-line bg-surface px-2 text-xs text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-orange/60"
            >
              <option value="">Reclassify as…</option>
              {ELEMENT_TYPES.filter((t) => t !== element.element_type).map((t) => (
                <option key={t} value={t}>
                  {labelize(t)}
                </option>
              ))}
            </select>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              disabled={busy || reclassifyTo === ''}
              onClick={() => onDecide(item, { decision: 'correct', after: { element_type: reclassifyTo } })}
            >
              <Undo2 className="h-3.5 w-3.5" aria-hidden="true" />
              Apply
            </Button>
          </div>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={busy}
            className="border-brand-red/40 text-brand-red hover:bg-brand-red/10"
            onClick={() => onDecide(item, { decision: 'dismiss' })}
          >
            <X className="h-3.5 w-3.5" aria-hidden="true" />
            Reject as trap
          </Button>
        </div>
      )}
    </Card>
  )
}
