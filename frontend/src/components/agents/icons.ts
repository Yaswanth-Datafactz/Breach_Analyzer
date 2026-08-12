import { Bot, ClipboardCheck, Scale, SearchCheck } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import type { AgentKind } from '../../lib/api/types'

/** One Lucide icon per agent kind (docs/plan.md §3's four agents). */
export const AGENT_ICONS: Record<AgentKind, LucideIcon> = {
  orchestrator: Bot,
  investigator: SearchCheck,
  adjudicator: Scale,
  auditor: ClipboardCheck,
}
