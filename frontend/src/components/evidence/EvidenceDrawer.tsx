import { useEffect } from 'react'
import { ChevronRight, FileSearch, X } from 'lucide-react'
import { Button } from '../ui/Button'
import { Pill } from '../ui/Pill'
import { ConfidenceMeter } from '../confidence/ConfidenceMeter'
import { EmptyState } from '../common/EmptyState'
import { labelize } from '../../lib/format'
import type { FlagDetail } from '../../lib/api/types'
import type { PassageTarget } from './PassageViewer'

/** docs/plan.md §7: flag click opens this drawer listing every evidence
 * reference behind the flag; a reference click opens the PassageViewer on
 * the exact passage and offsets. No flag renders without at least one row
 * here -- that invariant is enforced server-side (services/exposure.py). */
export function EvidenceDrawer({
  flag,
  onOpenPassage,
  onClose,
}: {
  flag: FlagDetail
  onOpenPassage: (target: PassageTarget) => void
  onClose: () => void
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-40" role="dialog" aria-modal="true" aria-label="Flag evidence">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <aside className="absolute right-0 top-0 flex h-full w-full max-w-md flex-col border-l border-line bg-surface-raised shadow-lg">
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div>
            <p className="text-sm font-semibold text-ink">{labelize(flag.category)} evidence</p>
            <div className="mt-1 flex items-center gap-3">
              <ConfidenceMeter confidence={flag.confidence} />
              <Pill tone="neutral">{labelize(flag.review_status)}</Pill>
            </div>
          </div>
          <Button type="button" variant="ghost" size="icon" onClick={onClose} aria-label="Close">
            <X className="h-4 w-4" aria-hidden="true" />
          </Button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {flag.evidence.length === 0 ? (
            <EmptyState
              icon={FileSearch}
              title="No evidence references"
              body="This flag carries no passage-anchored references, which violates the evidence invariant. Flag it to your teams."
            />
          ) : (
            <ul className="flex flex-col gap-2">
              {flag.evidence.map((reference) => (
                <li key={reference.id}>
                  <button
                    type="button"
                    onClick={() =>
                      onOpenPassage({
                        passageId: reference.passage_id,
                        charStart: reference.char_start,
                        charEnd: reference.char_end,
                        documentFilename: reference.document_filename,
                        label: `${labelize(reference.element_type)} evidence`,
                      })
                    }
                    className="group flex w-full items-center gap-3 rounded-card border border-line bg-surface px-4 py-3 text-left transition-colors hover:border-brand-orange/60"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium text-ink">
                          {labelize(reference.element_type)}
                        </span>
                        {reference.detector ? (
                          <Pill tone="neutral">{reference.detector}</Pill>
                        ) : null}
                        {reference.validation_status && reference.validation_status !== 'valid' ? (
                          <Pill tone="danger">{labelize(reference.validation_status)}</Pill>
                        ) : null}
                      </div>
                      <p className="mt-1 truncate text-xs text-ink-muted">
                        {reference.document_filename ?? 'document'}
                      </p>
                      {reference.snippet ? (
                        <p className="mt-1 line-clamp-2 text-xs text-ink-muted">
                          {reference.snippet}
                        </p>
                      ) : null}
                    </div>
                    <ChevronRight
                      className="h-4 w-4 shrink-0 text-ink-muted transition-colors group-hover:text-brand-orange"
                      aria-hidden="true"
                    />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>
    </div>
  )
}
