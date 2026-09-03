import { useRuntimeConfig, useState } from '#app'
import { createApiClient, type ApiClient } from '~/lib/apiClient'

export { ApiError } from '~/lib/apiClient'
export type { ApiClient, QueryParams, RequestOptions } from '~/lib/apiClient'

/** Token JWT de Cognito. Compartido por `useApi` y el store `auth`. */
export function useAuthToken() {
  return useState<string | null>('auth.token', () => null)
}

/** Marca puesta por el cliente cuando el backend contesta 401. */
export function useAuthExpired() {
  return useState<boolean>('auth.expired', () => false)
}

/**
 * Cliente tipado de la API. `baseURL` sale de `runtimeConfig.public.apiBase`,
 * el Bearer de la sesión y los errores llegan siempre como `ApiError`.
 */
export function useApi(): ApiClient {
  const config = useRuntimeConfig()
  const token = useAuthToken()
  const expired = useAuthExpired()

  return createApiClient({
    baseUrl: String(config.public.apiBase),
    getToken: () => token.value,
    onUnauthorized: () => {
      token.value = null
      expired.value = true
    },
  })
}
