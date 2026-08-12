import { cn } from '../../lib/cn'
import { confidenceBand } from '../confidence/bands'
import { labelize } from '../../lib/format'
import type { FlagOut } from '../../lib/api/types'

/** Category pill with the flag's confidence band as a leading dot -- the
 * band vocabulary of ConfidenceMeter (high = quiet grey, medium = yellow,
 * low = red; color never carries the identity, the label does). */
const BAND_DOT: Record<string, string> = {
  high: 'bg-ink-muted',
  medium: 'bg-brand-yellow',
  low: 'bg-brand-red',
}

export function FlagPill({ flag, onClick }: { flag: FlagOut; onClick?: () => void }) {
  const band = confidenceBand(flag.confidence)
  const title = `${labelize(flag.category)} — confidence ${
    flag.confidence === null ? 'n/a' : flag.confidence.toFixed(2)
  }, ${flag.evidence_count} evidence ${flag.evidence_count === 1 ? 'reference' : 'references'}`
  const body = (
    <>
      <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', BAND_DOT[band])} aria-hidden="true" />
      {labelize(flag.category)}
    </>
  )
  const base =
    'inline-flex items-center gap-1.5 rounded-full border border-line bg-surface px-2.5 py-1 text-xs font-medium text-ink'
  if (!onClick) {
    return (
      <span className={base} title={title}>
        {body}
      </span>
    )
  }
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={cn(base, 'transition-colors hover:border-brand-orange/60 hover:text-brand-orange')}
    >
      {body}
    </button>
  )
}
