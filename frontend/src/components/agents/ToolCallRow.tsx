import { useState } from 'react'
import { ChevronDown, ChevronRight, TriangleAlert, Wrench } from 'lucide-react'
import { cn } from '../../lib/cn'
import { formatLatency } from '../../lib/format'
import type { AgentToolCall } from '../../lib/api/types'

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="min-w-0">
      <p className="mb-1 text-xs font-medium uppercase tracking-wide text-ink-muted">{label}</p>
      <pre className="overflow-x-auto rounded-btn border border-line bg-surface px-3 py-2 text-xs leading-5 text-ink">
        {JSON.stringify(value ?? null, null, 2)}
      </pre>
    </div>
  )
}

/** One persisted tool call (agent_tool_calls row): name, args, result
 * summary, latency; errored calls carry the danger accent (docs/plan.md §3:
 * every call is committed before its handler runs, so what renders here is
 * exactly what the agent did -- including the failures). */
export function ToolCallRow({ call }: { call: AgentToolCall }) {
  const [open, setOpen] = useState(false)
  return (
    <div
      className={cn(
        'rounded-btn border bg-surface',
        call.is_error ? 'border-brand-red/50' : 'border-line',
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-ink-muted" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-ink-muted" aria-hidden="true" />
        )}
        {call.is_error ? (
          <TriangleAlert className="h-3.5 w-3.5 shrink-0 text-brand-red" aria-hidden="true" />
        ) : (
          <Wrench className="h-3.5 w-3.5 shrink-0 text-ink-muted" aria-hidden="true" />
        )}
        <code className={cn('text-xs font-medium', call.is_error ? 'text-brand-red' : 'text-ink')}>
          {call.tool_name}
        </code>
        {call.is_error ? (
          <span className="text-xs text-brand-red">returned an error</span>
        ) : null}
        <span className="ml-auto shrink-0 text-xs tabular-nums text-ink-muted">
          {formatLatency(call.latency_ms)}
        </span>
      </button>
      {open ? (
        <div className="grid gap-3 border-t border-line px-3 py-3 sm:grid-cols-2">
          <JsonBlock label="Arguments" value={call.args} />
          <JsonBlock label="Result summary" value={call.result_summary} />
        </div>
      ) : null}
    </div>
  )
}
