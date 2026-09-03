import { computed, ref, shallowRef } from 'vue'
import { useApi } from './useApi'
import { ApiError } from '~/lib/apiClient'
import { DEFAULT_LICENSE, LICENSES, licenseFacts } from '~/lib/licensing'
import type { License, LicenseCode, LicenseResolution } from '~/types/domain'

/**
 * Catálogo de licencias. Arranca con la tabla local (docs/licensing.md) para
 * que el formulario sea usable al instante, y se sincroniza con
 * `GET /licenses` cuando llega la respuesta.
 */
export function useLicenses() {
  const api = useApi()
  const remote = shallowRef<License[] | null>(null)
  const pending = ref(false)
  const error = ref<ApiError | null>(null)

  const catalog = computed(() =>
    LICENSES.map((facts) => {
      const server = remote.value?.find((l) => l.code === facts.code)
      return {
        ...facts,
        url: server?.url ?? facts.url,
        allowsCommercial: server?.allows_commercial ?? facts.allowsCommercial,
        allowsDerivatives: server?.allows_derivatives ?? facts.allowsDerivatives,
        requiresAttribution: server?.requires_attribution ?? facts.requiresAttribution,
        requiresShareAlike: server?.requires_sharealike ?? facts.requiresShareAlike,
        restrictiveness: server?.restrictiveness ?? facts.restrictiveness,
      }
    }),
  )

  async function load() {
    if (pending.value || remote.value) return
    pending.value = true
    error.value = null
    try {
      remote.value = await api.get<License[]>('/licenses', undefined, { anonymous: true })
    } catch (e) {
      // No es fatal: la tabla local ya es correcta y el aviso se registra.
      error.value = e instanceof ApiError ? e : new ApiError({ status: 0, title: 'unknown_error' })
    } finally {
      pending.value = false
    }
  }

  /**
   * `POST /licenses/resolve` — la misma función de dominio que usa el motor de
   * reconstrucción. El frontend nunca calcula esto por su cuenta para decidir.
   */
  function resolve(photoIds: string[]) {
    return api.post<LicenseResolution>('/licenses/resolve', { photo_ids: photoIds })
  }

  return {
    catalog,
    pending,
    error,
    load,
    resolve,
    defaultLicense: DEFAULT_LICENSE,
    facts: (code: LicenseCode) => licenseFacts(code),
  }
}
