// Base URL/key are overridable via frontend/.env (see .env.example); the
// defaults match the backend's own dev defaults (backend/.env.example)
// purely so the app works out of the box in local dev.
const DEFAULT_BASE_URL = 'http://localhost:8000/api/v1'
const DEFAULT_API_KEY = 'changeme-dev-key'
const REQUEST_TIMEOUT_MS = 20_000

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? DEFAULT_BASE_URL
const API_KEY = import.meta.env.VITE_API_KEY ?? DEFAULT_API_KEY

export class ApiError extends Error {
  status: number
  type: string

  constructor(message: string, status: number, type = 'error') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.type = type
  }
}

export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  return { 'X-API-Key': API_KEY, ...extra }
}

export async function parseErrorBody(response: Response): Promise<{ type: string; message: string }> {
  try {
    const body = (await response.json()) as Partial<{ error: { type: string; message: string } }>
    if (body.error?.message) {
      return { type: body.error.type ?? 'error', message: body.error.message }
    }
  } catch {
    // Response body wasn't JSON (or was empty) -- fall through below.
  }
  return { type: 'error', message: `Request failed with status ${response.status}` }
}

/** JSON fetch wrapper: attaches X-API-Key, and turns the backend's
 * centralized {"error": {"type","message"}} shape into a typed ApiError
 * instead of a generic non-2xx failure. */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const timeoutController = new AbortController()
  const timeoutId = setTimeout(() => timeoutController.abort(), REQUEST_TIMEOUT_MS)

  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      signal: init?.signal ?? timeoutController.signal,
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
        ...init?.headers,
      },
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError('The server took too long to respond. Please try again.', 0, 'timeout')
    }
    throw new ApiError('Could not reach the server. Check your connection and try again.', 0, 'network')
  } finally {
    clearTimeout(timeoutId)
  }

  if (!response.ok) {
    const { type, message } = await parseErrorBody(response)
    throw new ApiError(message, response.status, type)
  }

  if (response.status === 204) {
    return undefined as T
  }

  return (await response.json()) as T
}
