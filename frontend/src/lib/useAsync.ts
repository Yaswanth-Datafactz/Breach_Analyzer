import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from './api/client'

export interface AsyncState<T> {
  data: T | null
  loading: boolean
  error: ApiError | null
  reload: () => void
  /** Locally patch already-loaded data (optimistic updates). */
  mutate: (updater: (current: T) => T) => void
}

function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err
  return new ApiError(err instanceof Error ? err.message : 'Unexpected error', 0)
}

/** The one data hook every page uses: loading + error + retry on every async
 * call (Handbook §6.2), abort on unmount/param change so no state is written
 * after the caller is gone. `deps` are the request parameters. */
export function useAsync<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<ApiError | null>(null)
  const [tick, setTick] = useState(0)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  useEffect(() => {
    const controller = new AbortController()
    let alive = true
    setLoading(true)
    setError(null)
    fetcherRef.current(controller.signal).then(
      (result) => {
        if (!alive) return
        setData(result)
        setLoading(false)
      },
      (err: unknown) => {
        if (!alive || controller.signal.aborted) return
        setError(toApiError(err))
        setLoading(false)
      },
    )
    return () => {
      alive = false
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- deps are the request params
  }, [...deps, tick])

  const reload = useCallback(() => setTick((t) => t + 1), [])
  const mutate = useCallback((updater: (current: T) => T) => {
    setData((current) => (current === null ? current : updater(current)))
  }, [])

  return { data, loading, error, reload, mutate }
}
