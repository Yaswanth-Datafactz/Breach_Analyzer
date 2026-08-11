import { cn } from '../../lib/cn'
import { confidenceBarClass } from './bands'

/** docs/plan.md: `absent_confirmed` renders "—", NEVER "0.00" -- a
 * confirmed absence is not a low-confidence value, it's a different claim
 * entirely and must not look like one on the meter. */
export function ConfidenceMeter({
  confidence,
  presence,
  className,
}: {
  confidence: number | null
  presence?: string
  className?: string
}) {
  if (presence === 'absent_confirmed' || confidence === null) {
    return <span className={cn('text-sm text-ink-muted', className)}>—</span>
  }

  return (
    <div className={cn('flex items-center gap-2', className)}>
      <div className="h-1.5 w-16 shrink-0 overflow-hidden rounded-full bg-line">
        <div
          className={cn('h-full rounded-full', confidenceBarClass(confidence))}
          style={{ width: `${Math.round(confidence * 100)}%` }}
        />
      </div>
      <span className="text-sm tabular-nums text-ink">{confidence.toFixed(2)}</span>
    </div>
  )
}
