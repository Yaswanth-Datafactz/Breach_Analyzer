import type { HTMLAttributes } from 'react'
import { cn } from '../../lib/cn'

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Handbook §7: cards lift on hover by default. Set false for compact,
   * densely-stacked rows (e.g. a conversation list) where translateY on
   * every row reads as jittery rather than as affordance. */
  hoverLift?: boolean
}

/** Handbook §7: rounded-xl (12px) cards. Use for clickable/actionable
 * surfaces -- not for passive content like chat bubbles, which use their
 * own bubble shape. */
export function Card({ className, hoverLift = true, ...props }: CardProps) {
  return (
    <div
      className={cn(
        'rounded-card border border-line bg-surface-raised transition-all duration-200',
        hoverLift && 'hover:-translate-y-1.25 hover:shadow-lg',
        className,
      )}
      {...props}
    />
  )
}
