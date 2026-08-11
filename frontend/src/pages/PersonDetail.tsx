import { useParams } from 'react-router-dom'
import { UserRound } from 'lucide-react'
import { Card } from '../components/ui/Card'
import { Pill } from '../components/ui/Pill'

/** Placeholder for Phase 0. The real identity card, alias chips, flags grid
 * with EvidenceDrawer and PassageViewer, and per-link ER rationale panel
 * (docs/plan.md §7) replace this body. */
export function PersonDetailPage() {
  const { personId } = useParams<{ personId: string }>()

  return (
    <div className="flex h-full items-center justify-center p-6">
      <Card hoverLift={false} className="flex max-w-md flex-col items-center gap-3 p-8 text-center">
        <UserRound className="h-8 w-8 text-ink-muted" />
        <p className="text-sm text-ink-muted">
          Identity, aliases, and passage-anchored evidence for this person render here once the
          pipeline has produced results.
        </p>
        <Pill tone="neutral">person {personId}</Pill>
      </Card>
    </div>
  )
}
