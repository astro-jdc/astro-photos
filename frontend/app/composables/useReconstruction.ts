import { onScopeDispose, ref, shallowRef } from 'vue'
import { useApi } from './useApi'
import { ApiError } from '~/lib/apiClient'
import type {
  PageReconstruction,
  ReconstructionInput,
  Reconstruction,
  ReconstructionEvent,
  ReconstructionPreview,
  ReconstructionRequest,
  ReconstructionResult,
} from '~/types/domain'

export function useReconstructions() {
  const api = useApi()
  return {
    list: (params: { limit?: number; cursor?: string | null; mine?: boolean } = {}) =>
      api.get<PageReconstruction>(
        '/reconstructions',
        { limit: params.limit ?? 24, cursor: params.cursor, mine: params.mine },
        { anonymous: !params.mine },
      ),
  }
}

/**
 * Una reconstrucción concreta: estado, procedencia, resultado y progreso en
 * vivo por SSE (`GET /reconstructions/{id}/events`).
 */
export function useReconstruction() {
  const api = useApi()

  const job = shallowRef<Reconstruction | null>(null)
  const inputs = shallowRef<ReconstructionInput[] | null>(null)
  const result = shallowRef<ReconstructionResult | null>(null)
  const lastEvent = shallowRef<ReconstructionEvent | null>(null)
  const progress = ref(0)
  const streaming = ref(false)
  const error = ref<ApiError | null>(null)

  let source: EventSource | null = null

  async function load(id: string) {
    error.value = null
    try {
      job.value = await api.get<Reconstruction>(`/reconstructions/${id}`)
      progress.value = job.value.progress
      if (job.value.status === 'succeeded') await loadResult(id)
    } catch (e) {
      error.value = e instanceof ApiError ? e : new ApiError({ status: 0, title: 'unknown_error' })
    }
  }

  async function loadInputs(id: string) {
    try {
      inputs.value = await api.get<ReconstructionInput[]>(`/reconstructions/${id}/inputs`)
    } catch (e) {
      error.value = e instanceof ApiError ? e : new ApiError({ status: 0, title: 'unknown_error' })
    }
  }

  async function loadResult(id: string) {
    try {
      result.value = await api.get<ReconstructionResult>(`/reconstructions/${id}/result`, undefined, {
        anonymous: true,
      })
    } catch (e) {
      error.value = e instanceof ApiError ? e : new ApiError({ status: 0, title: 'unknown_error' })
    }
  }

  /** Progreso en vivo. Degrada en silencio si no hay EventSource (SSR). */
  function connect(id: string, onFinished?: () => void) {
    if (typeof EventSource === 'undefined') return
    disconnect()
    source = new EventSource(api.url(`/reconstructions/${id}/events`))
    streaming.value = true

    source.onmessage = (message: MessageEvent<string>) => {
      let parsed: ReconstructionEvent
      try {
        parsed = JSON.parse(message.data) as ReconstructionEvent
      } catch {
        return
      }
      lastEvent.value = parsed
      progress.value = parsed.progress
      if (job.value) job.value = { ...job.value, status: parsed.status, progress: parsed.progress }
      if (parsed.status === 'succeeded' || parsed.status === 'failed' || parsed.status === 'cancelled') {
        disconnect()
        onFinished?.()
      }
    }

    source.onerror = () => {
      // El navegador reintenta solo; solo dejamos de anunciar "en directo".
      streaming.value = false
    }
  }

  function disconnect() {
    source?.close()
    source = null
    streaming.value = false
  }

  onScopeDispose(disconnect)

  return {
    job,
    inputs,
    result,
    lastEvent,
    progress,
    streaming,
    error,
    load,
    loadInputs,
    loadResult,
    connect,
    disconnect,
    /** Obligatorio antes de dejar lanzar nada (regla 4 del agente). */
    preview: (body: ReconstructionRequest) =>
      api.post<ReconstructionPreview>('/reconstructions/preview', body),
    create: (body: ReconstructionRequest) => api.post<Reconstruction>('/reconstructions', body),
    cancel: (id: string) => api.del<undefined>(`/reconstructions/${id}`),
  }
}
