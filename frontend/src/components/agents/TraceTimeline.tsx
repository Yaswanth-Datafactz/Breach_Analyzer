import { useState } from 'react'
import { ChevronDown, ChevronRight, CircleDot } from 'lucide-react'
import { Pill } from '../ui/Pill'
import { ToolCallRow } from './ToolCallRow'
import { formatInt, formatLatency, formatUsd } from '../../lib/format'
import type { AgentStep } from '../../lib/api/types'

function summaryText(summary: Record<string, unknown> | null): string | null {
  if (!summary) return null
  const text = summary.text ?? summary.prompt ?? summary.tool_result
  return typeof text === 'string' ? text : JSON.stringify(summary)
}

function StepRow({ step, defaultOpen }: { step: AgentStep; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const response = summaryText(step.response_summary)
  return (
    <li className="relative pl-8">
      <span className="absolute left-0 top-1.5 flex h-5 w-5 items-center justify-center">
        <CircleDot className="h-4 w-4 text-brand-orange" aria-hidden="true" />
      </span>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center gap-2 rounded-btn px-1 py-1 text-left"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-ink-muted" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-ink-muted" aria-hidden="true" />
        )}
        <span className="text-sm font-medium text-ink">Step {step.step_no}</span>
        {step.stop_reason ? <Pill tone="neutral">{step.stop_reason}</Pill> : null}
        {step.tool_calls.length > 0 ? (
          <span className="text-xs text-ink-muted">
            {step.tool_calls.length} tool {step.tool_calls.length === 1 ? 'call' : 'calls'}
          </span>
        ) : null}
        <span className="ml-auto flex shrink-0 items-center gap-3 text-xs tabular-nums text-ink-muted">
          <span>
            {formatInt(step.tokens_in)} in / {formatInt(step.tokens_out)} out
          </span>
          <span>{formatLatency(step.latency_ms)}</span>
          <span>{formatUsd(step.cost_usd)}</span>
        </span>
      </button>
      {open ? (
        <div className="mt-2 flex flex-col gap-2 pb-2">
          {response ? <p className="text-sm leading-6 text-ink-muted">{response}</p> : null}
          {step.tool_calls.map((call) => (
            <ToolCallRow key={call.id} call={call} />
          ))}
        </div>
      ) : null}
    </li>
  )
}

/** The persisted trace, step by step (agent_steps + agent_tool_calls --
 * committed BEFORE execution, so a crashed or parked run still shows its
 * full history here). */
export function TraceTimeline({ steps }: { steps: AgentStep[] }) {
  return (
    <ol className="flex flex-col gap-3 border-l border-line pl-1 [&>li]:-ml-px">
      {steps.map((step, index) => (
        <StepRow key={step.id} step={step} defaultOpen={index === steps.length - 1} />
      ))}
    </ol>
  )
}
