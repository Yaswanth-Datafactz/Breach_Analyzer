import { MeterBar } from '../common/MeterBar'
import { formatInt, formatUsd } from '../../lib/format'

export interface BudgetView {
  max_steps: number | null
  max_tokens: number | null
  max_usd: number | null
  steps_used: number
  tokens_used: number
  cost_usd: number
}

/** Used-vs-max for each budgeted dimension (docs/plan.md §3: budgets are
 * steps/tokens/USD). The bar turns to the danger hue only at 100% -- an
 * exhausted budget is the exception state the trace UI must make loud. */
export function BudgetMeter({ budget, compact = false }: { budget: BudgetView; compact?: boolean }) {
  const rows: Array<{ label: string; value: number; max: number | null; display: string }> = [
    {
      label: 'Steps',
      value: budget.steps_used,
      max: budget.max_steps,
      display: `${formatInt(budget.steps_used)} of ${formatInt(budget.max_steps)}`,
    },
    {
      label: 'Tokens',
      value: budget.tokens_used,
      max: budget.max_tokens,
      display: `${formatInt(budget.tokens_used)} of ${formatInt(budget.max_tokens)}`,
    },
    {
      label: 'Spend',
      value: budget.cost_usd,
      max: budget.max_usd,
      display: `${formatUsd(budget.cost_usd)} of ${formatUsd(budget.max_usd)}`,
    },
  ]
  const shown = compact ? rows.filter((row) => row.max !== null) : rows
  return (
    <div className="flex w-full flex-col gap-1.5">
      {shown.map((row) =>
        row.max === null ? (
          <div key={row.label} className="flex items-center gap-3">
            <span className="w-36 shrink-0 text-sm text-ink-muted">{row.label}</span>
            <span className="min-w-0 flex-1 text-xs text-ink-muted">no cap set</span>
            <span className="w-32 shrink-0 text-right text-sm tabular-nums text-ink">
              {row.label === 'Spend' ? formatUsd(row.value) : formatInt(row.value)}
            </span>
          </div>
        ) : (
          <MeterBar
            key={row.label}
            label={row.label}
            value={row.value}
            max={row.max}
            display={row.display}
            tone={row.value >= row.max ? 'danger' : 'brand'}
          />
        ),
      )}
    </div>
  )
}
