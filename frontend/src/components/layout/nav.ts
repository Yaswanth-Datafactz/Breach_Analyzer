import { LayoutDashboard, Table2, ClipboardCheck, Bot } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

export interface NavItem {
  label: string
  to: string
  icon: LucideIcon
  /** Custom active-match predicate, for items whose page covers more than
   * one route (Exposure covers the person drill-down at "/persons/:id",
   * Agents covers "/agents/runs/:id"). Falls back to an exact pathname
   * match when omitted. */
  isActive?: (pathname: string) => boolean
}

export const NAV_ITEMS: NavItem[] = [
  {
    label: 'Dashboard',
    to: '/',
    icon: LayoutDashboard,
  },
  {
    label: 'Exposure',
    to: '/exposure',
    icon: Table2,
    isActive: (pathname) => pathname.startsWith('/exposure') || pathname.startsWith('/persons'),
  },
  {
    label: 'Review',
    to: '/review',
    icon: ClipboardCheck,
  },
  {
    label: 'Agents',
    to: '/agents',
    icon: Bot,
    isActive: (pathname) => pathname.startsWith('/agents'),
  },
]

// Resolved per-route in App.tsx's Layout() via useLocation() -- AppShell
// itself takes one pageTitle string and stays byte-identical to UC1/UC2's
// copy, so per-route titles are handled here instead of adding a prop to
// the shared shell (docs/plan.md D7).
export const PAGE_TITLES: Record<string, string> = {
  '/': 'Run Dashboard',
  '/exposure': 'Exposure Table',
  '/review': 'Review Queue',
  '/agents': 'Agent Operations',
}

export function resolvePageTitle(pathname: string): string {
  if (pathname.startsWith('/persons/')) return 'Person detail'
  if (pathname.startsWith('/agents/runs/')) return 'Agent run'
  return PAGE_TITLES[pathname] ?? 'Breach Analytics'
}
