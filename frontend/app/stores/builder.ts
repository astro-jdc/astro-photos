import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { ApiError } from '~/lib/apiClient'
import { useReconstruction } from '~/composables/useReconstruction'
import { resolveOutputLicenseHint } from '~/lib/licensing'
import type {
  PhotoSummary,
  Reconstruction,
  ReconstructionPreview,
  ReconstructionRequest,
} from '~/types/domain'

const STORAGE_KEY = 'astro-photos.builder'

export const PIPELINES = [
  { id: 'classical-stack-v1', learned: false },
  { id: 'drizzle-v1', learned: false },
  { id: 'burst-sr-v1', learned: true },
] as const

export type PipelineId = (typeof PIPELINES)[number]['id']

/**
 * El "carrito" de frames de una reconstrucción.
 *
 * Invariante de producto (regla 4 del agente): `canLaunch` solo es cierto tras
 * un `POST /reconstructions/preview` exitoso sobre la selección **actual**.
 * Cualquier cambio en la selección invalida el preview.
 */
export const useBuilderStore = defineStore('builder', () => {
  const { preview: previewApi, create } = useReconstruction()

  const frames = ref<PhotoSummary[]>([])
  const objectId = ref<string | null>(null)
  const pipeline = ref<PipelineId>('classical-stack-v1')
  const params = ref<Record<string, unknown>>({})

  const previewRaw = ref<ReconstructionPreview | null>(null)
  const previewPending = ref(false)
  const previewError = ref<ApiError | null>(null)
  /** Hash de la selección con la que se calculó `preview`. */
  const previewSignature = ref<string | null>(null)

  const submitting = ref(false)
  const submitError = ref<ApiError | null>(null)

  const ids = computed(() => frames.value.map((f) => f.id))
  const count = computed(() => frames.value.length)
  const signature = computed(() => `${pipeline.value}|${[...ids.value].sort().join(',')}`)
  /** El plan comprobado ya no corresponde a la selección actual. */
  const isStale = computed(
    () => previewRaw.value !== null && previewSignature.value !== signature.value,
  )
  /**
   * El plan **vigente**. En cuanto la selección cambia deja de existir: así es
   * imposible encolar un job contra un plan que ya no se corresponde con lo
   * que el usuario tiene delante.
   */
  const preview = computed<ReconstructionPreview | null>(() =>
    isStale.value ? null : previewRaw.value,
  )

  const usesLearnedModel = computed(
    () => PIPELINES.find((p) => p.id === pipeline.value)?.learned ?? false,
  )

  /** Pista local mientras el servidor contesta. Nunca decide por sí sola. */
  const licenseHint = computed(() =>
    resolveOutputLicenseHint(
      frames.value.map((f) => ({
        license: f.license,
        allowDerivativesInStacks: f.allow_derivatives_in_stacks,
      })),
    ),
  )

  const canPreview = computed(() => frames.value.length >= 2 && !previewPending.value)
  const canLaunch = computed(
    () => preview.value !== null && preview.value.selected.length >= 2 && !submitting.value,
  )

  function has(id: string): boolean {
    return frames.value.some((f) => f.id === id)
  }

  function add(photo: PhotoSummary) {
    if (has(photo.id)) return
    frames.value = [...frames.value, photo]
    if (!objectId.value && photo.object_id) objectId.value = photo.object_id
  }

  function toggle(photo: PhotoSummary) {
    if (has(photo.id)) remove(photo.id)
    else add(photo)
  }

  function remove(id: string) {
    frames.value = frames.value.filter((f) => f.id !== id)
  }

  function clear() {
    frames.value = []
    previewRaw.value = null
    previewSignature.value = null
    previewError.value = null
    submitError.value = null
  }

  function buildRequest(): ReconstructionRequest {
    return {
      object_id: objectId.value,
      photo_ids: ids.value,
      selector: null,
      pipeline: pipeline.value,
      params: params.value,
    }
  }

  async function runPreview(): Promise<ReconstructionPreview | null> {
    if (frames.value.length === 0) return null
    previewPending.value = true
    previewError.value = null
    const forSignature = signature.value
    try {
      const result = await previewApi(buildRequest())
      previewRaw.value = result
      previewSignature.value = forSignature
      return result
    } catch (e) {
      previewRaw.value = null
      previewSignature.value = null
      previewError.value =
        e instanceof ApiError ? e : new ApiError({ status: 0, title: 'unknown_error' })
      return null
    } finally {
      previewPending.value = false
    }
  }

  /** Quita de la selección todas las fotos que el servidor marcó bloqueadas. */
  function dropBlocked() {
    const blocked = new Set(previewRaw.value?.blocked.map((b) => b.photo_id) ?? [])
    if (blocked.size === 0) return
    frames.value = frames.value.filter((f) => !blocked.has(f.id))
  }

  async function launch(): Promise<Reconstruction | null> {
    if (!canLaunch.value) return null
    submitting.value = true
    submitError.value = null
    try {
      return await create(buildRequest())
    } catch (e) {
      submitError.value =
        e instanceof ApiError ? e : new ApiError({ status: 0, title: 'unknown_error' })
      return null
    } finally {
      submitting.value = false
    }
  }

  function persist() {
    if (typeof localStorage === 'undefined') return
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ frames: frames.value, objectId: objectId.value, pipeline: pipeline.value }),
    )
  }

  function restore() {
    if (typeof localStorage === 'undefined') return
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (!raw) return
      const parsed = JSON.parse(raw) as {
        frames?: PhotoSummary[]
        objectId?: string | null
        pipeline?: PipelineId
      }
      frames.value = parsed.frames ?? []
      objectId.value = parsed.objectId ?? null
      if (parsed.pipeline && PIPELINES.some((p) => p.id === parsed.pipeline)) {
        pipeline.value = parsed.pipeline
      }
    } catch {
      // Un carrito corrupto se descarta sin drama.
    }
  }

  // El carrito sobrevive a una recarga; el plan comprobado, no.
  watch(signature, () => persist())

  return {
    frames,
    objectId,
    pipeline,
    params,
    preview,
    previewPending,
    previewError,
    submitting,
    submitError,
    ids,
    count,
    isStale,
    usesLearnedModel,
    licenseHint,
    canPreview,
    canLaunch,
    has,
    add,
    toggle,
    remove,
    clear,
    buildRequest,
    runPreview,
    dropBlocked,
    launch,
    persist,
    restore,
  }
})
