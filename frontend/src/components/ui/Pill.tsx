import type { HTMLAttributes } from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '../../lib/cn'

const pillVariants = cva('inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium', {
  variants: {
    tone: {
      brand: 'bg-brand-orange/10 text-brand-orange',
      neutral: 'border border-line bg-surface text-ink-muted',
      danger: 'bg-brand-red/10 text-brand-red',
    },
  },
  defaultVariants: { tone: 'brand' },
})

export interface PillProps extends HTMLAttributes<HTMLSpanElement>, VariantProps<typeof pillVariants> {}

export function Pill({ className, tone, ...props }: PillProps) {
  return <span className={cn(pillVariants({ tone }), className)} {...props} />
}
