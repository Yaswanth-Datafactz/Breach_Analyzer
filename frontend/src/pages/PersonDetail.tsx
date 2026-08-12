import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, CalendarDays, FileStack, Link2, UserRound } from 'lucide-react'
import { Card } from '../components/ui/Card'
import { Pill } from '../components/ui/Pill'
import { Spinner } from '../components/ui/Spinner'
import { ErrorBanner } from '../components/ui/ErrorBanner'
import { ConfidenceMeter } from '../components/confidence/ConfidenceMeter'
import { EmptyState } from '../components/common/EmptyState'
import { StatusPill } from '../components/common/StatusPill'
import { FlagPill } from '../components/exposure/FlagPill'
import { EvidenceDrawer } from '../components/evidence/EvidenceDrawer'
import { PassageViewer, type PassageTarget } from '../components/evidence/PassageViewer'
import { useAsync } from '../lib/useAsync'
import { getPerson } from '../lib/api/fetchers'
import { formatDate, formatInt, labelize } from '../lib/format'
import type { FlagDetail } from '../lib/api/types'

/** docs/plan.md §7's Person detail: identity card (best name, alias chips
 * with variant kinds, DOB), flags grid -> EvidenceDrawer -> PassageViewer
 * with the char-offset highlight, plus the ER panel with per-link
 * method/rule/score/rationale. */
export function PersonDetailPage() {
  const { personId } = useParams<{ personId: string }>()
  const { data, loading, error, reload } = useAsync(
    (signal) => getPerson(personId ?? '', signal),
    [personId],
  )
  const [openFlag, setOpenFlag] = useState<FlagDetail | null>(null)
  const [passageTarget, setPassageTarget] = useState<PassageTarget | null>(null)

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
  if (!data) return null

  const exposedFlags = data.flags.filter((flag) => flag.exposed)

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-5xl flex-col gap-4">
        <Link
          to="/exposure"
          className="flex w-fit items-center gap-1.5 text-sm text-ink-muted transition-colors hover:text-brand-orange"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          Back to exposure table
        </Link>

        <Card hoverLift={false} className="p-5">
          <div className="flex flex-wrap items-start gap-4">
            <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-brand-orange/10">
              <UserRound className="h-6 w-6 text-brand-orange" aria-hidden="true" />
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-lg font-semibold text-ink">{data.best_name}</h2>
                <StatusPill status={data.review_status} />
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-ink-muted">
                <span className="flex items-center gap-1.5">
                  <CalendarDays className="h-4 w-4" aria-hidden="true" />
                  {data.dob ? formatDate(data.dob) : 'DOB unknown'}
                </span>
                <span className="flex items-center gap-1.5">
                  <FileStack className="h-4 w-4" aria-hidden="true" />
                  {formatInt(data.document_count)} documents · {formatInt(data.mention_count)}{' '}
                  mentions
                </span>
                <span className="flex items-center gap-2">
                  ER confidence
                  <ConfidenceMeter confidence={data.er_confidence} />
                </span>
              </div>
              {data.aliases.length > 0 ? (
                <div className="mt-3 flex flex-wrap items-center gap-1.5">
                  <span className="text-xs text-ink-muted">Also appears as</span>
                  {data.aliases.map((alias) => (
                    <Pill key={alias.name} tone="neutral" title={`variant kind: ${alias.kind}`}>
                      {alias.name}
                      <span className="text-ink-muted">· {labelize(alias.kind)}</span>
                    </Pill>
                  ))}
                </div>
              ) : null}
            </div>
          </div>
        </Card>

        <section>
          <h3 className="mb-2 text-sm font-semibold text-ink">Exposed categories</h3>
          {exposedFlags.length === 0 ? (
            <Card hoverLift={false}>
              <EmptyState
                icon={FileStack}
                title="No exposure flags"
                body="No validated element of this person's mentions maps to an exposure category."
              />
            </Card>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {exposedFlags.map((flag) => (
                <Card
                  key={flag.id}
                  className="cursor-pointer p-4"
                  onClick={() => setOpenFlag(flag)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      setOpenFlag(flag)
                    }
                  }}
                >
                  <div className="flex items-center justify-between gap-2">
                    <FlagPill flag={flag} />
                    <span className="text-xs text-ink-muted">
                      {formatInt(flag.evidence_count)}{' '}
                      {flag.evidence_count === 1 ? 'reference' : 'references'}
                    </span>
                  </div>
                  <div className="mt-3 flex items-center justify-between gap-2">
                    <ConfidenceMeter confidence={flag.confidence} />
                    <StatusPill status={flag.review_status} />
                  </div>
                </Card>
              ))}
            </div>
          )}
        </section>

        <section>
          <h3 className="mb-2 text-sm font-semibold text-ink">Identity resolution</h3>
          <Card hoverLift={false} className="p-1">
            {data.identity_links.length === 0 ? (
              <EmptyState
                icon={Link2}
                title="No identity links"
                body="This person carries no mention links, which should not happen for a resolved identity."
              />
            ) : (
              <ul className="flex flex-col">
                {data.identity_links.map((link) => (
                  <li
                    key={link.id}
                    className="flex flex-col gap-1 border-b border-line/60 px-4 py-3 last:border-0"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <Link2 className="h-4 w-4 shrink-0 text-ink-muted" aria-hidden="true" />
                      <span className="text-sm font-medium text-ink">
                        {link.mention_name_raw ?? 'mention'}
                      </span>
                      <span className="truncate text-xs text-ink-muted">
                        {link.document_filename ?? ''}
                      </span>
                      <span className="ml-auto flex shrink-0 items-center gap-2">
                        <Pill tone={link.method === 'agent' ? 'brand' : 'neutral'}>
                          {link.method}
                          {link.rule_id ? ` · ${link.rule_id}` : ''}
                        </Pill>
                        {link.score !== null ? (
                          <span className="text-xs tabular-nums text-ink">
                            {link.score.toFixed(2)}
                          </span>
                        ) : null}
                        {!link.active ? <Pill tone="danger">inactive</Pill> : null}
                      </span>
                    </div>
                    {link.rationale ? (
                      <p className="pl-6 text-xs leading-5 text-ink-muted">{link.rationale}</p>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </section>
      </div>

      {openFlag ? (
        <EvidenceDrawer
          flag={openFlag}
          onClose={() => setOpenFlag(null)}
          onOpenPassage={(target) => setPassageTarget(target)}
        />
      ) : null}
      {passageTarget ? (
        <PassageViewer target={passageTarget} onClose={() => setPassageTarget(null)} />
      ) : null}
    </div>
  )
}
