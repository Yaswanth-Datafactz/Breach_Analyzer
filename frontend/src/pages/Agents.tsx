import { Bot } from 'lucide-react'
import { Card } from '../components/ui/Card'
import { Pill } from '../components/ui/Pill'

/** Placeholder for Phase 0. The real runs list with budget meters, the
 * TraceTimeline with expandable tool calls, and the ApprovalBanner
 * (docs/plan.md §7) replace this body. Covers both /agents and
 * /agents/runs/:agentRunId, mirroring UC2's Review route pattern. */
export function AgentsPage() {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <Card hoverLift={false} className="flex max-w-md flex-col items-center gap-3 p-8 text-center">
        <Bot className="h-8 w-8 text-ink-muted" />
        <p className="text-sm text-ink-muted">
          No agent runs yet. Budgeted, fully traced agent activity, including pending approvals,
          appears here.
        </p>
        <Pill tone="neutral">no runs recorded</Pill>
      </Card>
    </div>
  )
}
