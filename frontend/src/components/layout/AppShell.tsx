import type { ReactNode } from 'react'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'
import type { NavItem } from './nav'

/** The shared shell (header/nav/content chrome) meant to be reused,
 * unchanged, by Use Cases 2 and 3: pass this use case's own nav items,
 * product name, and page title/content, and it renders the same branded
 * frame every use case shares. See docs/phase-5.md for the exact reuse
 * pattern. */
export function AppShell({
  navItems,
  productName,
  pageTitle,
  sidebarExtra,
  topBarActions,
  children,
}: {
  navItems: NavItem[]
  productName: string
  pageTitle: string
  sidebarExtra?: ReactNode
  topBarActions?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="flex h-screen w-screen overflow-hidden bg-surface text-ink">
      <Sidebar navItems={navItems} productName={productName}>
        {sidebarExtra}
      </Sidebar>
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar title={pageTitle} actions={topBarActions} />
        <main className="min-h-0 flex-1 overflow-hidden">{children}</main>
      </div>
    </div>
  )
}
