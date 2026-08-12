import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { cn } from '../../lib/cn'

/** The one branded empty-state block: used for genuinely-empty datasets AND
 * for expected not-yet states (no cost rows before an LLM run, no accuracy
 * runs before the eval phase). Never used to hide an error. */
export function EmptyState({
  icon: Icon,
  title,
  body,
  action,
  className,
}: {
  icon: LucideIcon
  title: string
  body?: string
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-col items-center gap-2 px-6 py-10 text-center', className)}>
      <Icon className="h-7 w-7 text-ink-muted" aria-hidden="true" />
      <p className="text-sm font-medium text-ink">{title}</p>
      {body ? <p className="max-w-md text-sm text-ink-muted">{body}</p> : null}
      {action}
    </div>
  )
}
