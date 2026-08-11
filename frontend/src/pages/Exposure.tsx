import { Users } from 'lucide-react'
import { Card } from '../components/ui/Card'
import { Pill } from '../components/ui/Pill'

/** Placeholder for Phase 0. The real exposure table -- person rows, flag
 * pills with confidence bands, search/filter, exports (docs/plan.md §7) --
 * replaces this body. */
export function ExposurePage() {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <Card hoverLift={false} className="flex max-w-md flex-col items-center gap-3 p-8 text-center">
        <Users className="h-8 w-8 text-ink-muted" />
        <p className="text-sm text-ink-muted">
          No exposure rows yet. When a run completes, every affected person appears here with
          evidence-backed category flags your teams can defend.
        </p>
        <Pill tone="neutral">awaiting first run</Pill>
      </Card>
    </div>
  )
}
