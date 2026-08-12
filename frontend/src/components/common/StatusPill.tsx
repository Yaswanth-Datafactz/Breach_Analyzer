import { Pill } from '../ui/Pill'
import { labelize } from '../../lib/format'

/** Domain status -> pill tone. Danger is reserved for states a reviewer must
 * catch (failed, exceeded, rejected, quarantined); brand marks live/pending
 * attention; everything settled stays quiet neutral -- same philosophy as
 * the confidence bands (good outcomes never shout). */
const DANGER = new Set(['failed', 'budget_exceeded', 'quarantined', 'rejected', 'escalated'])
const BRAND = new Set(['running', 'awaiting_approval', 'pending', 'needs_review', 'open'])

export function StatusPill({ status }: { status: string }) {
  const tone = DANGER.has(status) ? 'danger' : BRAND.has(status) ? 'brand' : 'neutral'
  return <Pill tone={tone}>{labelize(status)}</Pill>
}
