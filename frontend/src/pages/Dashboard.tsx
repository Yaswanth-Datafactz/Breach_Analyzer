import { useSearchParams } from 'react-router-dom'
import {
  Coins,
  FileWarning,
  Files,
  Gauge,
  Layers,
  ScanSearch,
  ShieldAlert,
  Target,
  Users,
} from 'lucide-react'
import { Card } from '../components/ui/Card'
import { Pill } from '../components/ui/Pill'
import { Spinner } from '../components/ui/Spinner'
import { ErrorBanner } from '../components/ui/ErrorBanner'
import { EmptyState } from '../components/common/EmptyState'
import { MeterBar } from '../components/common/MeterBar'
import { StatusPill } from '../components/common/StatusPill'
import { useAsync } from '../lib/useAsync'
import { ApiError } from '../lib/api/client'
import {
  getCostSummary,
  getExposure,
  getRun,
  listAccuracyRuns,
  listDocuments,
  listRuns,
  listRunQuarantines,
} from '../lib/api/fetchers'
import { formatDateTime, formatInt, formatPct, formatUsd, labelize } from '../lib/format'
import type {
  AccuracyRun,
  CostSummary,
  DocumentOut,
  Quarantine,
  Run,
} from '../lib/api/types'

interface DashboardData {
  run: Run
  personsTotal: number | null
  quarantines: Quarantine[]
  costs: CostSummary | null
  accuracy: AccuracyRun[] | null
  documents: DocumentOut[]
}

/** The populated run is the one the exposure table serves (the deployed app
 * serves exactly one populated run -- exposure.py's own default); before ER
 * has produced persons, fall back to the newest run that actually saw files. */
async function resolveRunId(signal: AbortSignal, override: string | null): Promise<{
  runId: string | null
  personsTotal: number | null
}> {
  if (override) return { runId: override, personsTotal: null }
  try {
    const exposure = await getExposure({ limit: 1 }, signal)
    return { runId: exposure.run_id, personsTotal: exposure.total }
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 404)) throw err
  }
  const runs = await listRuns(50, signal)
  const withFiles = runs.items.find((run) => (run.counters?.files_seen ?? 0) > 0)
  return { runId: (withFiles ?? runs.items[0])?.id ?? null, personsTotal: null }
}

async function loadDashboard(
  signal: AbortSignal,
  override: string | null,
): Promise<DashboardData | null> {
  const { runId, personsTotal } = await resolveRunId(signal, override)
  if (runId === null) return null
  const run = await getRun(runId, signal)
  // cost_events exist exactly when a model tier was called (§9: every call
  // writes one) -- gating on the run's own counters keeps the no-spend state
  // a 200-only flow instead of an expected 422 Chrome would log red.
  const llmCalls = (run.counters?.tier1_calls ?? 0) + (run.counters?.tier2_calls ?? 0)
  const [quarantines, costs, accuracy, documents] = await Promise.all([
    listRunQuarantines(runId, signal),
    llmCalls > 0 ? getCostSummary(runId, signal) : Promise.resolve(null),
    listAccuracyRuns(signal),
    listDocuments({ limit: 2000 }, signal),
  ])
  return {
    run,
    personsTotal,
    quarantines: quarantines.items,
    costs,
    accuracy,
    documents: documents.items.filter((doc) => doc.run_id === runId),
  }
}

function StatTile({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: typeof Files
  label: string
  value: string
  hint?: string
}) {
  return (
    <Card className="flex items-center gap-3 px-4 py-3">
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-btn bg-brand-orange/10">
        <Icon className="h-4.5 w-4.5 text-brand-orange" aria-hidden="true" />
      </span>
      <div className="min-w-0">
        <p className="truncate text-xs text-ink-muted">{label}</p>
        <p className="text-lg font-semibold tabular-nums text-ink">{value}</p>
        {hint ? <p className="truncate text-xs text-ink-muted">{hint}</p> : null}
      </div>
    </Card>
  )
}

function FunnelCard({ run }: { run: Run }) {
  const c = run.counters ?? {}
  const max = Math.max(c.files_seen ?? 0, c.ingested ?? 0, 1)
  const stages: Array<{ label: string; value: number; tone?: 'brand' | 'danger' | 'muted' }> = [
    { label: 'Files seen', value: c.files_seen ?? 0 },
    { label: 'Ingested', value: c.ingested ?? 0 },
    { label: 'Parsed', value: c.parsed ?? 0 },
    { label: 'Done', value: c.done ?? 0 },
    { label: 'Quarantined', value: c.quarantined ?? 0, tone: 'danger' },
    { label: 'Failed', value: c.failed ?? 0, tone: 'danger' },
  ]
  return (
    <Card hoverLift={false} className="p-5">
      <h2 className="mb-1 text-sm font-semibold text-ink">Run funnel</h2>
      <p className="mb-4 text-xs text-ink-muted">
        Every document ends done or quarantined with a reason — nothing is silently dropped.
      </p>
      <div className="flex flex-col gap-2">
        {stages.map((stage) => (
          <MeterBar
            key={stage.label}
            label={stage.label}
            value={stage.value}
            max={max}
            display={formatInt(stage.value)}
            tone={stage.tone ?? (stage.value === max ? 'brand' : 'brand')}
          />
        ))}
      </div>
      <p className="mt-3 text-xs text-ink-muted">
        {formatInt(c.deduped ?? 0)} duplicate files deduplicated by content hash ·{' '}
        {formatInt(c.attachments ?? 0)} email attachments re-ingested as child documents ·{' '}
        {formatInt(c.ocr_documents ?? 0)} documents recovered through OCR
      </p>
    </Card>
  )
}

function ClassTable({ documents }: { documents: DocumentOut[] }) {
  const byClass = new Map<string, { total: number; done: number; quarantined: number; image: number }>()
  for (const doc of documents) {
    const row = byClass.get(doc.file_class) ?? { total: 0, done: 0, quarantined: 0, image: 0 }
    row.total += 1
    if (doc.status === 'done') row.done += 1
    if (doc.status === 'quarantined') row.quarantined += 1
    if (doc.is_image_based) row.image += 1
    byClass.set(doc.file_class, row)
  }
  const rows = [...byClass.entries()].sort((a, b) => b[1].total - a[1].total)
  return (
    <Card hoverLift={false} className="p-5">
      <h2 className="mb-1 text-sm font-semibold text-ink">Documents by class</h2>
      <p className="mb-3 text-xs text-ink-muted">
        Per-format outcome for this run, including attachments.
      </p>
      <div className="overflow-x-auto">
        <table className="w-full min-w-md text-sm">
          <thead>
            <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-ink-muted">
              <th className="py-2 pr-3 font-medium">Class</th>
              <th className="py-2 pr-3 text-right font-medium">Documents</th>
              <th className="py-2 pr-3 text-right font-medium">Done</th>
              <th className="py-2 pr-3 text-right font-medium">Quarantined</th>
              <th className="py-2 text-right font-medium">Image-based</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([fileClass, row]) => (
              <tr key={fileClass} className="border-b border-line/60 last:border-0">
                <td className="py-2 pr-3 font-medium text-ink">{fileClass}</td>
                <td className="py-2 pr-3 text-right tabular-nums text-ink">{formatInt(row.total)}</td>
                <td className="py-2 pr-3 text-right tabular-nums text-ink">{formatInt(row.done)}</td>
                <td className="py-2 pr-3 text-right tabular-nums">
                  <span className={row.quarantined > 0 ? 'text-brand-red' : 'text-ink'}>
                    {formatInt(row.quarantined)}
                  </span>
                </td>
                <td className="py-2 text-right tabular-nums text-ink">{formatInt(row.image)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

function TierCard({ costs, run }: { costs: CostSummary | null; run: Run }) {
  const tier0Elements = run.counters?.tier0_elements ?? 0
  return (
    <Card hoverLift={false} className="p-5">
      <h2 className="mb-1 text-sm font-semibold text-ink">Tier usage and cost</h2>
      <p className="mb-4 text-xs text-ink-muted">
        Tier 0 deterministic detectors run free on every passage; model tiers are measured from
        cost_events.
      </p>
      <div className="mb-3 flex items-center justify-between rounded-btn border border-line bg-surface px-3 py-2">
        <span className="text-sm text-ink">Tier 0 detectors</span>
        <span className="text-sm tabular-nums text-ink">
          {formatInt(tier0Elements)} elements · $0.00
        </span>
      </div>
      {costs === null ? (
        <EmptyState
          icon={Coins}
          title="No model spend recorded for this run"
          body="Tier-1 and tier-2 extraction have not run yet — the corpus is processed to tier-0 depth. Once the keyed extraction run happens, per-tier calls, tokens, cache hit rates, and cost render here from cost_events."
          className="py-6"
        />
      ) : (
        <div className="flex flex-col gap-2">
          {costs.rows.map((row) => (
            <MeterBar
              key={`${row.purpose}-${row.model}`}
              label={`${labelize(row.purpose)} (${row.model})`}
              value={row.cost_usd}
              max={Math.max(costs.totals.cost_usd, 0.000001)}
              display={`${formatInt(row.calls)} calls · ${formatUsd(row.cost_usd)}`}
            />
          ))}
          <p className="mt-2 text-xs text-ink-muted">
            Total {formatUsd(costs.totals.cost_usd)} · cache hit rate{' '}
            {formatPct(costs.totals.cache_hit_rate, 1)}
          </p>
        </div>
      )}
    </Card>
  )
}

function QuarantineCard({ quarantines }: { quarantines: Quarantine[] }) {
  return (
    <Card hoverLift={false} className="p-5">
      <h2 className="mb-1 text-sm font-semibold text-ink">Quarantines</h2>
      <p className="mb-3 text-xs text-ink-muted">
        Documents that could not be processed, each with its reason on record.
      </p>
      {quarantines.length === 0 ? (
        <EmptyState icon={ShieldAlert} title="No quarantined documents" className="py-6" />
      ) : (
        <ul className="flex flex-col gap-2">
          {quarantines.map((q) => (
            <li
              key={q.id}
              className="flex flex-wrap items-center gap-2 rounded-btn border border-line bg-surface px-3 py-2"
            >
              <FileWarning className="h-4 w-4 shrink-0 text-brand-red" aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate text-sm text-ink" title={q.document_rel_path}>
                {q.document_filename}
              </span>
              <Pill tone="danger">{labelize(q.reason_code)}</Pill>
              <Pill tone="neutral">{q.stage}</Pill>
              <StatusPill status={q.status} />
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

function AccuracyCard({ accuracy }: { accuracy: AccuracyRun[] | null }) {
  return (
    <Card hoverLift={false} className="p-5">
      <h2 className="mb-1 text-sm font-semibold text-ink">Accuracy snapshot</h2>
      <p className="mb-3 text-xs text-ink-muted">
        Measured against the corpus ground-truth manifest (person P/R, per-flag accuracy).
      </p>
      {accuracy === null || accuracy.length === 0 ? (
        <EmptyState
          icon={Target}
          title="No accuracy evaluation yet"
          body="The evaluation harness runs after the full extraction pass and writes accuracy_runs; its person precision/recall and per-flag scorecard render here."
          className="py-6"
        />
      ) : (
        <div className="flex flex-col gap-2">
          {accuracy.slice(0, 3).map((run) => (
            <div key={run.id} className="rounded-btn border border-line bg-surface px-3 py-2">
              <p className="text-xs text-ink-muted">{formatDateTime(run.created_at)}</p>
              <pre className="mt-1 overflow-x-auto text-xs text-ink">
                {JSON.stringify(run.metrics, null, 2)}
              </pre>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

export function DashboardPage() {
  const [searchParams] = useSearchParams()
  const runOverride = searchParams.get('run')
  const { data, loading, error, reload } = useAsync(
    (signal) => loadDashboard(signal, runOverride),
    [runOverride],
  )

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="h-7 w-7" />
      </div>
    )
  }
  if (error) {
    return (
      <div className="p-6">
        <ErrorBanner message={error.message} onRetry={reload} />
      </div>
    )
  }
  if (!data) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <Card hoverLift={false} className="flex max-w-md flex-col items-center gap-3 p-8 text-center">
          <Gauge className="h-8 w-8 text-ink-muted" />
          <p className="text-sm text-ink-muted">
            No processing run yet. Once a run starts, its funnel, tier usage, and measured cost
            appear here.
          </p>
          <Pill tone="neutral">awaiting first run</Pill>
        </Card>
      </div>
    )
  }

  const { run } = data
  const wallSeconds =
    run.started_at && run.finished_at
      ? Math.round((new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000)
      : null

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-6xl flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-base font-semibold text-ink">
            Run {run.id.slice(0, 8)}
          </h2>
          <StatusPill status={run.status} />
          <span className="text-xs text-ink-muted">
            started {formatDateTime(run.started_at)}
            {wallSeconds !== null ? ` · ${wallSeconds}s wall` : ''}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          <StatTile
            icon={Files}
            label="Documents"
            value={formatInt(run.counters?.ingested ?? 0)}
            hint={`${formatInt(run.counters?.attachments ?? 0)} attachments`}
          />
          <StatTile icon={Layers} label="Passages" value={formatInt(run.counters?.passages ?? 0)} />
          <StatTile
            icon={ScanSearch}
            label="Tier-0 elements"
            value={formatInt(run.counters?.tier0_elements ?? 0)}
            hint="deterministic, $0"
          />
          <StatTile
            icon={Users}
            label="Persons resolved"
            value={data.personsTotal === null ? '—' : formatInt(data.personsTotal)}
            hint="entity resolution"
          />
          <StatTile
            icon={ShieldAlert}
            label="Quarantined"
            value={formatInt(run.counters?.quarantined ?? 0)}
            hint="with recorded reasons"
          />
        </div>

        <div className="grid gap-4 xl:grid-cols-2">
          <FunnelCard run={run} />
          <TierCard costs={data.costs} run={run} />
        </div>

        <ClassTable documents={data.documents} />

        <div className="grid gap-4 xl:grid-cols-2">
          <QuarantineCard quarantines={data.quarantines} />
          <AccuracyCard accuracy={data.accuracy} />
        </div>
      </div>
    </div>
  )
}
