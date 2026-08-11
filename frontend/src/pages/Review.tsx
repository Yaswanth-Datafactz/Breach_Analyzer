import { ClipboardCheck } from 'lucide-react'
import { Card } from '../components/ui/Card'
import { Pill } from '../components/ui/Pill'

/** Placeholder for Phase 0. The real extraction queue and ER-pair queue
 * with side-by-side evidence (docs/plan.md §7) replace this body. */
export function ReviewPage() {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <Card hoverLift={false} className="flex max-w-md flex-col items-center gap-3 p-8 text-center">
        <ClipboardCheck className="h-8 w-8 text-ink-muted" />
        <p className="text-sm text-ink-muted">
          Nothing awaiting review. Extraction and identity decisions that need your teams&apos;
          judgment queue here.
        </p>
        <Pill tone="neutral">queue empty</Pill>
      </Card>
    </div>
  )
}
