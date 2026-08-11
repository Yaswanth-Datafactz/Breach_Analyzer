// docs/plan.md's confidence display rules: high confidence renders quiet
// grey, NEVER green (no color in the DataFactZ palette is green, and
// coloring the good state teaches a reviewer to skim past the exception
// they're there to catch). Only the bar/pill carries color; the numeral
// is always text-ink (both brand hues fail WCAG contrast as text on a
// light background).

export type ConfidenceBand = 'high' | 'medium' | 'low'

export function confidenceBand(confidence: number | null): ConfidenceBand {
  if (confidence === null) return 'low'
  if (confidence >= 0.8) return 'high'
  if (confidence >= 0.5) return 'medium'
  return 'low'
}

const BAND_BAR_CLASS: Record<ConfidenceBand, string> = {
  high: 'bg-ink-muted',
  medium: 'bg-brand-yellow',
  low: 'bg-brand-red',
}

export function confidenceBarClass(confidence: number | null): string {
  return BAND_BAR_CLASS[confidenceBand(confidence)]
}
