import { computed, ref, shallowRef, type Ref } from 'vue'
import { useApi } from './useApi'
import { ApiError } from '~/lib/apiClient'
import { DEFAULT_PAGE_SIZE, toQuery, type PhotoFilters } from '~/lib/photoQuery'
import type { PagePhotoSummary, Photo, PhotoPatch, PhotoSummary } from '~/types/domain'

/**
 * Galería con scroll infinito por cursor (`?limit=&cursor=` →
 * `{items, next_cursor}`, docs/api.md).
 */
export function usePhotos(filters: Ref<PhotoFilters>) {
  const api = useApi()
  const items = shallowRef<PhotoSummary[]>([])
  const cursor = ref<string | null>(null)
  const pending = ref(false)
  const error = ref<ApiError | null>(null)
  const exhausted = ref(false)

  let requestId = 0

  async function load(reset: boolean) {
    if (pending.value) return
    if (!reset && exhausted.value) return
    const id = ++requestId
    pending.value = true
    error.value = null
    try {
      const query = toQuery({
        ...filters.value,
        limit: filters.value.limit ?? DEFAULT_PAGE_SIZE,
        cursor: reset ? null : cursor.value,
      })
      const page = await api.get<PagePhotoSummary>('/photos', query, { anonymous: true })
      if (id !== requestId) return
      items.value = reset ? page.items : [...items.value, ...page.items]
      cursor.value = page.next_cursor
      exhausted.value = page.next_cursor === null
    } catch (e) {
      if (id !== requestId) return
      error.value = e instanceof ApiError ? e : new ApiError({ status: 0, title: 'unknown_error' })
    } finally {
      if (id === requestId) pending.value = false
    }
  }

  return {
    items,
    pending,
    error,
    exhausted,
    isEmpty: computed(() => !pending.value && items.value.length === 0),
    refresh: () => load(true),
    loadMore: () => load(false),
  }
}

export function usePhoto() {
  const api = useApi()

  return {
    fetch: (id: string) => api.get<Photo>(`/photos/${id}`, undefined, { anonymous: true }),
    update: (id: string, patch: PhotoPatch) => api.patch<Photo>(`/photos/${id}`, patch),
    remove: (id: string) => api.del<undefined>(`/photos/${id}`),
    similar: (id: string, limit = 12) =>
      api.get<PagePhotoSummary>(`/photos/similar/${id}`, { limit }, { anonymous: true }),
    /** `GET /photos/{id}/download` responde 302 a una URL firmada de CloudFront. */
    downloadUrl: (id: string) => api.url(`/photos/${id}/download`),
  }
}
