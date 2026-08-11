import { AlertTriangle } from 'lucide-react'
import { Button } from './Button'
import { cn } from '../../lib/cn'

/** The one shared error-state component: every async call in this app
 * (and future use cases reusing this shell) renders its failures through
 * this, so errors are never swallowed and never look ad hoc. */
export function ErrorBanner({
  message,
  onRetry,
  className,
}: {
  message: string
  onRetry?: () => void
  className?: string
}) {
  return (
    <div
      role="alert"
      className={cn(
        'flex items-center justify-between gap-3 rounded-card border border-brand-red/30 bg-brand-red/10 px-4 py-3 text-sm text-brand-red',
        className,
      )}
    >
      <span className="flex items-center gap-2">
        <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
        {message}
      </span>
      {onRetry ? (
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={onRetry}
          className="border-brand-red/40 text-brand-red hover:bg-brand-red/10"
        >
          Try again
        </Button>
      ) : null}
    </div>
  )
}
