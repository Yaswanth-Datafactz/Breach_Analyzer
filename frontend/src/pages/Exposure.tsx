import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { ChevronLeft, ChevronRight, Download, Search, Users } from 'lucide-react'
import { Button } from '../components/ui/Button'
import { Card } from '../components/ui/Card'
import { ErrorBanner } from '../components/ui/ErrorBanner'
import { Spinner } from '../components/ui/Spinner'
import { ConfidenceMeter } from '../components/confidence/ConfidenceMeter'
import { EmptyState } from '../components/common/EmptyState'
import { StatusPill } from '../components/common/StatusPill'
import { FlagPill } from '../components/exposure/FlagPill'
import { useAsync } from '../lib/useAsync'
import { ApiError } from '../lib/api/client'
import {
  downloadAuthed,
  exposureCsvPath,
  exposureXlsxPath,
  getExposure,
} from '../lib/api/fetchers'
import { formatDate, formatInt, labelize } from '../lib/format'

const PAGE_SIZE = 25

const CATEGORIES = [
  'ssn',
  'dob',
  'drivers_license',
  'passport',
  'financial_account',
  'credit_card',
  'medical',
  'credentials',
  'address',
  'phone',
  'email',
]

const REVIEW_STATUSES = ['auto', 'needs_review', 'human_confirmed', 'human_overridden']

/** docs/plan.md §7's Exposure page: one row per resolved person, category
 * flag pills with confidence bands, doc counts, ER confidence, review
 * status; pagination/search/filters are all SERVER query params -- this is
 * the table that grows to millions of rows at 100x, nothing filters
 * client-side. */
export function ExposurePage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const search = searchParams.get('q') ?? ''
  const category = searchParams.get('category') ?? ''
  const reviewStatus = searchParams.get('review') ?? ''
  const page = Math.max(0, Number(searchParams.get('page') ?? '0') || 0)

  const [searchInput, setSearchInput] = useState(search)
  useEffect(() => setSearchInput(search), [search])
  useEffect(() => {
    const handle = setTimeout(() => {
      if (searchInput !== search) {
        setSearchParams(
          (params) => {
            const next = new URLSearchParams(params)
            if (searchInput) next.set('q', searchInput)
            else next.delete('q')
            next.delete('page')
            return next
          },
          { replace: true },
        )
      }
    }, 350)
    return () => clearTimeout(handle)
  }, [searchInput, search, setSearchParams])

  const queryParams = useMemo(
    () => ({
      search: search || undefined,
      category: category || undefined,
      review_status: reviewStatus || undefined,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    }),
    [search, category, reviewStatus, page],
  )

  const { data, loading, error, reload } = useAsync(
    (signal) => getExposure(queryParams, signal),
    [queryParams],
  )

  const [exporting, setExporting] = useState<'csv' | 'xlsx' | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)

  async function handleExport(kind: 'csv' | 'xlsx') {
    if (!data) return
    setExporting(kind)
    setExportError(null)
    try {
      const path = kind === 'csv' ? exposureCsvPath(data.run_id) : exposureXlsxPath(data.run_id)
      await downloadAuthed(path, `exposure-${data.run_id.slice(0, 8)}.${kind}`)
    } catch (err) {
      setExportError(err instanceof Error ? err.message : 'Export failed')
    } finally {
      setExporting(null)
    }
  }

  function setFilter(key: string, value: string) {
    setSearchParams((params) => {
      const next = new URLSearchParams(params)
      if (value) next.set(key, value)
      else next.delete(key)
      next.delete('page')
      return next
    })
  }

  function setPage(next: number) {
    setSearchParams((params) => {
      const nextParams = new URLSearchParams(params)
      if (next > 0) nextParams.set('page', String(next))
      else nextParams.delete('page')
      return nextParams
    })
  }

  const noPersonsYet = error instanceof ApiError && error.status === 404

  return (
    <div className="flex h-full flex-col overflow-y-auto p-6">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <label className="relative min-w-56 flex-1">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-muted"
              aria-hidden="true"
            />
            <input
              type="search"
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
              placeholder="Search name or alias"
              aria-label="Search name or alias"
              className="h-10 w-full rounded-btn border border-line bg-surface-raised pl-9 pr-3 text-sm text-ink placeholder:text-ink-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-orange/60"
            />
          </label>
          <select
            value={category}
            onChange={(event) => setFilter('category', event.target.value)}
            aria-label="Filter by exposure category"
            className="h-10 rounded-btn border border-line bg-surface-raised px-3 text-sm text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-orange/60"
          >
            <option value="">All categories</option>
            {CATEGORIES.map((value) => (
              <option key={value} value={value}>
                {labelize(value)}
              </option>
            ))}
          </select>
          <select
            value={reviewStatus}
            onChange={(event) => setFilter('review', event.target.value)}
            aria-label="Filter by review status"
            className="h-10 rounded-btn border border-line bg-surface-raised px-3 text-sm text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-orange/60"
          >
            <option value="">All review statuses</option>
            {REVIEW_STATUSES.map((value) => (
              <option key={value} value={value}>
                {labelize(value)}
              </option>
            ))}
          </select>
          <div className="ml-auto flex items-center gap-2">
            <Button
              type="button"
              variant="secondary"
              size="sm"
              disabled={exporting !== null || !data}
              onClick={() => handleExport('csv')}
            >
              {exporting === 'csv' ? <Spinner className="h-3.5 w-3.5" /> : <Download className="h-3.5 w-3.5" aria-hidden="true" />}
              CSV
            </Button>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              disabled={exporting !== null || !data}
              onClick={() => handleExport('xlsx')}
            >
              {exporting === 'xlsx' ? <Spinner className="h-3.5 w-3.5" /> : <Download className="h-3.5 w-3.5" aria-hidden="true" />}
              XLSX
            </Button>
          </div>
        </div>

        {exportError ? <ErrorBanner message={exportError} /> : null}

        {loading ? (
          <div className="flex justify-center py-16">
            <Spinner className="h-7 w-7" />
          </div>
        ) : noPersonsYet ? (
          <Card hoverLift={false}>
            <EmptyState
              icon={Users}
              title="No exposure rows yet"
              body="No run has resolved persons. When entity resolution completes, every affected individual appears here with evidence-backed category flags your teams can defend."
            />
          </Card>
        ) : error ? (
          <ErrorBanner message={error.message} onRetry={reload} />
        ) : data && data.items.length === 0 ? (
          <Card hoverLift={false}>
            <EmptyState
              icon={Users}
              title="No persons match these filters"
              body="Adjust the search or filters to widen the result set."
            />
          </Card>
        ) : data ? (
          <>
            <Card hoverLift={false} className="overflow-x-auto">
              <table className="w-full min-w-3xl text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-ink-muted">
                    <th className="px-4 py-3 font-medium">Person</th>
                    <th className="px-4 py-3 font-medium">Exposed categories</th>
                    <th className="px-4 py-3 text-right font-medium">Documents</th>
                    <th className="px-4 py-3 font-medium">ER confidence</th>
                    <th className="px-4 py-3 font-medium">Review</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((person) => (
                    <tr
                      key={person.id}
                      onClick={() => navigate(`/persons/${person.id}`)}
                      className="cursor-pointer border-b border-line/60 transition-colors last:border-0 hover:bg-surface"
                    >
                      <td className="px-4 py-3">
                        <p className="font-medium text-ink">{person.best_name}</p>
                        <p className="text-xs text-ink-muted">
                          {person.dob ? `DOB ${formatDate(person.dob)}` : 'DOB unknown'}
                          {person.aliases.length > 0
                            ? ` · ${person.aliases.length} ${person.aliases.length === 1 ? 'alias' : 'aliases'}`
                            : ''}
                        </p>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex max-w-md flex-wrap gap-1.5">
                          {person.flags.filter((flag) => flag.exposed).map((flag) => (
                            <FlagPill key={flag.id} flag={flag} />
                          ))}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-ink">
                        {formatInt(person.document_count)}
                        <span className="block text-xs text-ink-muted">
                          {formatInt(person.mention_count)} mentions
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <ConfidenceMeter confidence={person.er_confidence} />
                      </td>
                      <td className="px-4 py-3">
                        <StatusPill status={person.review_status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>

            <div className="flex items-center justify-between">
              <p className="text-xs text-ink-muted">
                Showing {formatInt(data.offset + 1)}–{formatInt(data.offset + data.items.length)} of{' '}
                {formatInt(data.total)} persons · run {data.run_id.slice(0, 8)}
              </p>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  disabled={page === 0}
                  onClick={() => setPage(page - 1)}
                >
                  <ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />
                  Previous
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  disabled={data.offset + data.items.length >= data.total}
                  onClick={() => setPage(page + 1)}
                >
                  Next
                  <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
                </Button>
              </div>
            </div>
          </>
        ) : null}
      </div>
    </div>
  )
}
