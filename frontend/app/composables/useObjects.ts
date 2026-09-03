import { ref, shallowRef } from 'vue'
import { useApi } from './useApi'
import { ApiError } from '~/lib/apiClient'
import type { ObjectCoverage, PageSkyObject, SkyObject } from '~/types/domain'

export function useObjects() {
  const api = useApi()

  return {
    list: (params: { q?: string; type?: string; limit?: number; cursor?: string | null } = {}) =>
      api.get<PageSkyObject>(
        '/objects',
        { q: params.q, type: params.type, limit: params.limit ?? 30, cursor: params.cursor },
        { anonymous: true },
      ),
    get: (id: string) => api.get<SkyObject>(`/objects/${id}`, undefined, { anonymous: true }),
  }
}

/**
 * Autocompletado de objetos para `MetadataForm`. Devuelve un buscador con
 * cancelación: la última pulsación gana.
 */
export function useObjectSearch() {
  const { list } = useObjects()
  const results = shallowRef<SkyObject[]>([])
  const pending = ref(false)
  const error = ref<ApiError | null>(null)
  let seq = 0

  async function search(term: string) {
    const id = ++seq
    if (term.trim().length < 1) {
      results.value = []
      return
    }
    pending.value = true
    error.value = null
    try {
      const page = await list({ q: term.trim(), limit: 12 })
      if (id !== seq) return
      results.value = page.items
    } catch (e) {
      if (id !== seq) return
      error.value = e instanceof ApiError ? e : new ApiError({ status: 0, title: 'unknown_error' })
      results.value = []
    } finally {
      if (id === seq) pending.value = false
    }
  }

  return { results, pending, error, search }
}

/**
 * `GET /objects/{id}/coverage` — cuántas fotos hay por celda de
 * tiempo x latitud x focal. Alimenta el widget de "faltan tomas desde el
 * hemisferio sur".
 */
export function useObjectCoverage() {
  const api = useApi()
  const coverage = shallowRef<ObjectCoverage | null>(null)
  const pending = ref(false)
  const error = ref<ApiError | null>(null)

  async function load(objectId: string) {
    pending.value = true
    error.value = null
    try {
      coverage.value = await api.get<ObjectCoverage>(
        `/objects/${objectId}/coverage`,
        undefined,
        { anonymous: true },
      )
    } catch (e) {
      error.value = e instanceof ApiError ? e : new ApiError({ status: 0, title: 'unknown_error' })
      coverage.value = null
    } finally {
      pending.value = false
    }
  }

  return { coverage, pending, error, load }
}
