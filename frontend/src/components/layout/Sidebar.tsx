import type { ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { cn } from '../../lib/cn'
import type { NavItem } from './nav'

/** The reusable chrome piece: brand mark, nav, and an optional
 * domain-specific slot (Use Case 1 passes a conversation list; other use
 * cases can pass whatever they need, or nothing). Sidebar itself has no
 * chat-specific knowledge. */
export function Sidebar({
  navItems,
  productName,
  children,
}: {
  navItems: NavItem[]
  productName: string
  children?: ReactNode
}) {
  const location = useLocation()

  return (
    <aside className="flex w-72 shrink-0 flex-col border-r border-line bg-surface-raised">
      <div className="px-5 pt-6 pb-4">
        <p className="bg-gradient-to-r from-brand-yellow via-brand-orange to-brand-red bg-clip-text text-lg font-semibold tracking-tight text-transparent">
          DataFactZ
        </p>
        <p className="text-sm text-ink-muted">{productName}</p>
      </div>

      <nav className="flex flex-col gap-1 px-3">
        {navItems.map((item) => {
          const Icon = item.icon
          const isActive = item.isActive ? item.isActive(location.pathname) : location.pathname === item.to
          return (
            <Link
              key={item.to}
              to={item.to}
              className={cn(
                'flex items-center gap-2 rounded-btn px-3 py-2 text-sm font-medium transition-colors',
                isActive ? 'bg-brand-orange/10 text-brand-orange' : 'text-ink-muted hover:bg-surface hover:text-ink',
              )}
            >
              <Icon className="h-4 w-4" aria-hidden="true" />
              {item.label}
            </Link>
          )
        })}
      </nav>

      {children ? (
        <div className="mt-4 flex min-h-0 flex-1 flex-col border-t border-line px-3 pt-4">{children}</div>
      ) : null}
    </aside>
  )
}
