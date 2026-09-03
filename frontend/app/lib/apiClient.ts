/**
 * Cliente HTTP tipado, sin dependencias de Nuxt para que sea testeable en
 * aislamiento. `app/composables/useApi.ts` lo envuelve con `runtimeConfig` y
 * el token de la sesión.
 *
 * Errores: el backend habla RFC 9457 (`application/problem+json`). Todo fallo
 * sale de aquí como `ApiError`, nunca como un `Response` crudo ni un `any`.
 */
import type { Problem, ProblemFieldError } from '~/types/domain'

export type QueryValue = string | number | boolean | null | undefined | (string | number)[]
export type QueryParams = Record<string, QueryValue>

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  query?: QueryParams
  body?: unknown
  headers?: Record<string, string>
  signal?: AbortSignal
  /** No adjuntar el Bearer aunque haya sesión (rutas 🔓). */
  anonymous?: boolean
  /** No seguir el 302 de /photos/{id}/download: queremos la Location. */
  redirect?: RequestRedirect
}

export class ApiError extends Error {
  readonly status: number
  readonly title: string
  readonly detail: string | null
  readonly type: string
  readonly instance: string | null
  readonly errors: ProblemFieldError[]
  /** Cuerpo problem+json completo, cuando lo hubo. */
  readonly problem: Problem | null

  constructor(init: {
    status: number
    title: string
    detail?: string | null
    type?: string
    instance?: string | null
    errors?: ProblemFieldError[]
    problem?: Problem | null
  }) {
    super(init.detail || init.title || `HTTP ${init.status}`)
    this.name = 'ApiError'
    this.status = init.status
    this.title = init.title
    this.detail = init.detail ?? null
    this.type = init.type ?? 'about:blank'
    this.instance = init.instance ?? null
    this.errors = init.errors ?? []
    this.problem = init.problem ?? null
  }

  /** 401/403: la UI debe mandar a iniciar sesión, no mostrar un error crudo. */
  get isAuthError(): boolean {
    return this.status === 401 || this.status === 403
  }

  get isNotFound(): boolean {
    return this.status === 404
  }

  /** 422 con `errors[]`: son errores por campo del formulario. */
  get fieldErrors(): Record<string, string> {
    const out: Record<string, string> = {}
    for (const e of this.errors) {
      const key = (e.field ?? '').replace(/^#?\/?(body\/)?/, '')
      if (key) out[key] = e.message
    }
    return out
  }
}

function isProblem(value: unknown): value is Problem {
  if (typeof value !== 'object' || value === null) return false
  const v = value as Record<string, unknown>
  return typeof v.title === 'string' && typeof v.status === 'number'
}

/** Convierte cualquier respuesta de error en `ApiError`. Exportada para tests. */
export async function toApiError(response: Response): Promise<ApiError> {
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('problem+json') || contentType.includes('application/json')) {
    let parsed: unknown
    try {
      parsed = await response.json()
    } catch {
      parsed = undefined
    }
    if (isProblem(parsed)) {
      return new ApiError({
        status: parsed.status || response.status,
        title: parsed.title,
        detail: parsed.detail ?? null,
        type: parsed.type,
        instance: parsed.instance ?? null,
        errors: parsed.errors ?? [],
        problem: parsed,
      })
    }
  }
  let text: string
  try {
    text = await response.text()
  } catch {
    text = ''
  }
  return new ApiError({
    status: response.status,
    title: response.statusText || `HTTP ${response.status}`,
    detail: text.slice(0, 500) || null,
  })
}

/** Serializa los parámetros de búsqueda: arrays como lista separada por comas. */
export function buildQuery(query: QueryParams | undefined): string {
  if (!query) return ''
  const sp = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === '') continue
    if (Array.isArray(value)) {
      if (value.length === 0) continue
      sp.set(key, value.join(','))
    } else {
      sp.set(key, String(value))
    }
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export function joinUrl(baseUrl: string, path: string): string {
  if (/^https?:\/\//i.test(path)) return path
  return `${baseUrl.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`
}

export interface ApiClientOptions {
  baseUrl: string
  /** Devuelve el JWT de Cognito, o null si no hay sesión. */
  getToken?: () => string | null | undefined
  /** Inyectable para tests. */
  fetchImpl?: typeof globalThis.fetch
  /** Cabeceras fijas (p. ej. Accept-Language). */
  defaultHeaders?: () => Record<string, string>
  /** Se llama en cada 401 para que el store de auth intente refrescar. */
  onUnauthorized?: () => void | Promise<void>
}

export interface ApiClient {
  request<T>(path: string, options?: RequestOptions): Promise<T>
  get<T>(path: string, query?: QueryParams, options?: RequestOptions): Promise<T>
  post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T>
  patch<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T>
  del<T>(path: string, options?: RequestOptions): Promise<T>
  /** URL absoluta de un recurso, para SSE o enlaces directos. */
  url(path: string, query?: QueryParams): string
}

export function createApiClient(options: ApiClientOptions): ApiClient {
  const doFetch = options.fetchImpl ?? globalThis.fetch

  async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    const url = joinUrl(options.baseUrl, path) + buildQuery(opts.query)

    const headers: Record<string, string> = {
      Accept: 'application/json',
      ...(options.defaultHeaders?.() ?? {}),
      ...(opts.headers ?? {}),
    }

    if (!opts.anonymous) {
      const token = options.getToken?.()
      if (token) headers.Authorization = `Bearer ${token}`
    }

    let payload: BodyInit | undefined
    if (opts.body !== undefined && opts.body !== null) {
      if (opts.body instanceof FormData || opts.body instanceof Blob) {
        payload = opts.body
      } else {
        headers['Content-Type'] = 'application/json'
        payload = JSON.stringify(opts.body)
      }
    }

    let response: Response
    try {
      response = await doFetch(url, {
        method: opts.method ?? 'GET',
        headers,
        body: payload,
        signal: opts.signal,
        redirect: opts.redirect,
        credentials: 'omit',
      })
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
      throw new ApiError({
        status: 0,
        title: 'network_error',
        detail: cause instanceof Error ? cause.message : String(cause),
      })
    }

    if (!response.ok) {
      const error = await toApiError(response)
      if (error.status === 401) await options.onUnauthorized?.()
      throw error
    }

    if (response.status === 204) return undefined as T
    const contentType = response.headers.get('content-type') ?? ''
    if (!contentType.includes('json')) return (await response.text()) as unknown as T
    return (await response.json()) as T
  }

  return {
    request,
    get: (path, query, opts) => request(path, { ...opts, method: 'GET', query }),
    post: (path, body, opts) => request(path, { ...opts, method: 'POST', body }),
    patch: (path, body, opts) => request(path, { ...opts, method: 'PATCH', body }),
    del: (path, opts) => request(path, { ...opts, method: 'DELETE' }),
    url: (path, query) => joinUrl(options.baseUrl, path) + buildQuery(query),
  }
}
