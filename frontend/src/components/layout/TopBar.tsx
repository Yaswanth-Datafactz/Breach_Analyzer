import type { ReactNode } from 'react'
import { ThemeToggle } from './ThemeToggle'

export function TopBar({ title, actions }: { title: string; actions?: ReactNode }) {
  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-line bg-surface-raised px-6">
      <h1 className="text-sm font-semibold tracking-tight text-ink">{title}</h1>
      <div className="flex items-center gap-2">
        {actions}
        <ThemeToggle />
      </div>
    </header>
  )
}
