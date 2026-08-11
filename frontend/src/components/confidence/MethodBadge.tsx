import { Pill } from '../ui/Pill'
import type { FieldOut } from '../../lib/api/types'

/** Names the provenance behind a confidence score -- docs/plan.md: this
 * badge IS the "don't fake a 0.95" disclosure. Priority follows
 * services/confidence.py's SIGNAL_WEIGHTS ordering (grounding > critic >
 * logprob > validator > agreement), so the badge names whichever signal
 * actually carried the most weight for this field, not an arbitrary pick. */
const SIGNAL_PRIORITY: Array<{ key: keyof FieldOut; label: string }> = [
  { key: 'signal_grounding', label: 'grounded' },
  { key: 'signal_critic', label: 'critic' },
  { key: 'signal_logprob', label: 'logprob' },
  { key: 'signal_validator', label: 'validated' },
  { key: 'signal_agreement', label: 'agreement' },
]

export function MethodBadge({ field }: { field: FieldOut }) {
  if (field.corrected) {
    return <Pill tone="neutral">manual</Pill>
  }
  if (field.presence !== 'extracted') {
    return null
  }
  const primary = SIGNAL_PRIORITY.find((signal) => field[signal.key] !== null)
  if (!primary) return <Pill tone="neutral">unverified</Pill>

  // A signal that RAN but scored low is a different claim than one that
  // scored high -- "grounded" alone would read as "yes, this is on the
  // page" even when grounding is exactly what caught it NOT being there.
  const value = field[primary.key] as number
  const label = value < 0.5 && primary.key === 'signal_grounding' ? 'ungrounded' : primary.label
  return <Pill tone="neutral">{label}</Pill>
}
