import { cn } from '../../lib/cn'

/** Single-hue magnitude bar (dataviz rules: one hue for magnitude, identity
 * comes from the row label, the numeral wears ink -- never the fill color;
 * brand-orange fails text contrast on light surfaces, so it is only ever a
 * fill). `tone` switches to the status hue for exception rows. */
export function MeterBar({
  label,
  value,
  max,
  display,
  tone = 'brand',
  className,
}: {
  label: string
  value: number
  max: number
  /** Right-hand numeral, e.g. "527" or "$0.08 of $0.50". */
  display: string
  tone?: 'brand' | 'danger' | 'muted'
  className?: string
}) {
  const fraction = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0
  const fill =
    tone === 'danger' ? 'bg-brand-red' : tone === 'muted' ? 'bg-ink-muted' : 'bg-brand-orange'
  return (
    <div className={cn('flex items-center gap-3', className)}>
      <span className="w-36 shrink-0 truncate text-sm text-ink-muted" title={label}>
        {label}
      </span>
      <div
        className="h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-line"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={value}
        aria-label={label}
      >
        <div
          className={cn('h-full rounded-full transition-all duration-300', fill)}
          style={{ width: `${fraction * 100}%` }}
        />
      </div>
      <span className="w-32 shrink-0 text-right text-sm tabular-nums text-ink">{display}</span>
    </div>
  )
}
