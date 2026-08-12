import { useEffect, useState } from 'react'
import { AlertTriangle, Download, FileText, X } from 'lucide-react'
import { Button } from '../ui/Button'
import { ErrorBanner } from '../ui/ErrorBanner'
import { Spinner } from '../ui/Spinner'
import { Pill } from '../ui/Pill'
import { useAsync } from '../../lib/useAsync'
import { getPassage, documentFilePath, downloadAuthed } from '../../lib/api/fetchers'
import { labelize } from '../../lib/format'
import type { Passage } from '../../lib/api/types'

export interface PassageTarget {
  passageId: string
  charStart: number | null
  charEnd: number | null
  documentFilename: string | null
  /** What the highlight anchors (for the header line), e.g. "ssn evidence". */
  label: string
}

function locatorCrumbs(passage: Passage): string[] {
  const locator = passage.locator ?? {}
  const crumbs: string[] = []
  if (passage.kind === 'page') {
    crumbs.push(`page ${locator.page ?? passage.seq + 1}`)
  } else if (passage.kind === 'sheet_range') {
    const sheet = typeof locator.sheet === 'string' ? locator.sheet : 'sheet'
    const rows =
      locator.row_start !== undefined && locator.row_end !== undefined
        ? ` rows ${locator.row_start}–${locator.row_end}`
        : ''
    crumbs.push(`sheet ${sheet}${rows}`)
  } else if (passage.kind === 'email_part') {
    crumbs.push(`email part ${locator.part ?? locator.section ?? passage.seq}`)
  } else {
    const paragraph = locator.paragraph ?? locator.block ?? null
    crumbs.push(paragraph !== null ? `paragraph ${paragraph}` : `block ${passage.seq}`)
  }
  if (passage.ocr) crumbs.push('OCR text')
  return crumbs
}

/** docs/plan.md §7: passage text with a <mark> at the evidence offsets, a
 * locator breadcrumb (filename -> page/sheet/part), and the original-file
 * link. Offsets that are missing or do not resolve inside the text render a
 * banner and the unhighlighted passage instead of a wrong highlight. */
export function PassageViewer({ target, onClose }: { target: PassageTarget; onClose: () => void }) {
  const { data, loading, error, reload } = useAsync(
    (signal) => getPassage(target.passageId, signal),
    [target.passageId],
  )
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const offsetsResolve =
    data !== null &&
    target.charStart !== null &&
    target.charEnd !== null &&
    target.charStart >= 0 &&
    target.charEnd > target.charStart &&
    target.charEnd <= data.text.length

  async function handleDownload() {
    if (!data) return
    setDownloading(true)
    setDownloadError(null)
    try {
      await downloadAuthed(
        documentFilePath(data.document_id),
        target.documentFilename ?? 'original',
      )
    } catch (err) {
      setDownloadError(err instanceof Error ? err.message : 'Download failed')
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6"
      role="dialog"
      aria-modal="true"
      aria-label="Passage evidence"
      onClick={onClose}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-3xl flex-col overflow-hidden rounded-card border border-line bg-surface-raised shadow-lg"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <p className="text-sm font-semibold text-ink">{target.label}</p>
            <p className="mt-0.5 flex flex-wrap items-center gap-1 text-xs text-ink-muted">
              <FileText className="h-3.5 w-3.5" aria-hidden="true" />
              <span className="truncate">{target.documentFilename ?? 'document'}</span>
              {data
                ? locatorCrumbs(data).map((crumb) => (
                    <span key={crumb} className="flex items-center gap-1">
                      <span aria-hidden="true">›</span>
                      {crumb}
                    </span>
                  ))
                : null}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={handleDownload}
              disabled={downloading || !data}
            >
              {downloading ? <Spinner className="h-3.5 w-3.5" /> : <Download className="h-3.5 w-3.5" aria-hidden="true" />}
              Original file
            </Button>
            <Button type="button" variant="ghost" size="icon" onClick={onClose} aria-label="Close">
              <X className="h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {loading ? (
            <div className="flex items-center justify-center py-10">
              <Spinner className="h-6 w-6" />
            </div>
          ) : error ? (
            <ErrorBanner message={error.message} onRetry={reload} />
          ) : data ? (
            <>
              {downloadError ? (
                <ErrorBanner message={downloadError} className="mb-3" />
              ) : null}
              {!offsetsResolve ? (
                <div className="mb-3 flex items-center gap-2 rounded-card border border-brand-yellow/40 bg-brand-yellow/10 px-4 py-3 text-sm text-ink">
                  <AlertTriangle className="h-4 w-4 shrink-0 text-brand-yellow" aria-hidden="true" />
                  Character offsets for this reference do not resolve inside the passage text, so
                  the value is not highlighted. The passage itself is shown in full.
                </div>
              ) : null}
              <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-6 text-ink">
                {offsetsResolve ? (
                  <>
                    {data.text.slice(0, target.charStart as number)}
                    <mark className="rounded bg-brand-orange/25 px-0.5 py-0.5 font-medium text-inherit ring-1 ring-brand-orange/50">
                      {data.text.slice(target.charStart as number, target.charEnd as number)}
                    </mark>
                    {data.text.slice(target.charEnd as number)}
                  </>
                ) : (
                  data.text
                )}
              </pre>
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <Pill tone="neutral">{labelize(data.kind)}</Pill>
                {offsetsResolve ? (
                  <Pill tone="neutral">
                    chars {target.charStart}–{target.charEnd}
                  </Pill>
                ) : null}
                {data.ocr ? <Pill tone="neutral">recovered via OCR</Pill> : null}
              </div>
            </>
          ) : null}
        </div>
      </div>
    </div>
  )
}
