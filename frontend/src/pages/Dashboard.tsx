import { Gauge } from 'lucide-react'
import { Card } from '../components/ui/Card'
import { Pill } from '../components/ui/Pill'

/** Placeholder for Phase 0. The real run funnel, tier hit-rate bars, and
 * cost-plus-extrapolation card (docs/plan.md §7) replace this body. */
export function DashboardPage() {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <Card hoverLift={false} className="flex max-w-md flex-col items-center gap-3 p-8 text-center">
        <Gauge className="h-8 w-8 text-ink-muted" />
        <p className="text-sm text-ink-muted">
          No processing run yet. Once a run starts, its funnel, tier hit rates, and measured cost
          appear here.
        </p>
        <Pill tone="neutral">awaiting first run</Pill>
      </Card>
    </div>
  )
}
