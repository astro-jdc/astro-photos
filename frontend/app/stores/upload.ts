import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '~/lib/apiClient'
import { useUpload } from '~/composables/useUpload'
import { readExifDraft, type ExifDraft } from '~/lib/exif'
import { inferMimeType, MULTIPART_THRESHOLD_BYTES } from '~/lib/upload'
import { DEFAULT_LICENSE, forbidsStackDerivatives } from '~/lib/licensing'
import type { LicenseCode, Photo, PhotoCompleteRequest } from '~/types/domain'

export type UploadState =
  | 'queued'
  | 'hashing'
  | 'requesting'
  | 'transferring'
  | 'completing'
  | 'done'
  | 'error'
  | 'cancelled'

export interface UploadItem {
  id: string
  file: File
  mimeType: string
  bytes: number
  multipart: boolean
  state: UploadState
  /** 0–1 */
  progress: number
  photoId: string | null
  photo: Photo | null
  exif: ExifDraft | null
  metadata: PhotoCompleteRequest
  errorTitle: string | null
  errorDetail: string | null
  attempts: number
}

const MAX_ATTEMPTS = 3
const RETRY_BASE_MS = 800

function newId(): string {
  const c = globalThis.crypto
  if (c && 'randomUUID' in c) return c.randomUUID()
  return `up_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
}

function blankMetadata(license: LicenseCode): PhotoCompleteRequest {
  return {
    title: null,
    description: null,
    license,
    captured_at_local: null,
    utc_offset_minutes: null,
    location: null,
    location_precision: 'city',
    object_id: null,
    site_id: null,
    equipment: {},
    attribution_name: null,
    allow_ai_training: true,
    allow_derivatives_in_stacks: !forbidsStackDerivatives(license),
  }
}

export const useUploadStore = defineStore('upload', () => {
  const { describe, ticket, transfer, complete } = useUpload()

  const items = ref<UploadItem[]>([])
  const controllers = new Map<string, AbortController>()

  const active = computed(() =>
    items.value.filter((i) => !['done', 'error', 'cancelled'].includes(i.state)),
  )
  const isBusy = computed(() => active.value.length > 0)
  const totalProgress = computed(() => {
    if (items.value.length === 0) return 0
    const sum = items.value.reduce((acc, i) => acc + i.progress, 0)
    return sum / items.value.length
  })
  const failed = computed(() => items.value.filter((i) => i.state === 'error'))
  const succeeded = computed(() => items.value.filter((i) => i.state === 'done'))

  function find(id: string): UploadItem | undefined {
    return items.value.find((i) => i.id === id)
  }

  /** Encola ficheros y lee su EXIF en paralelo para prerrellenar el formulario. */
  async function enqueue(files: File[], defaultLicense: LicenseCode = DEFAULT_LICENSE) {
    const created: UploadItem[] = files.map((file) => ({
      id: newId(),
      file,
      mimeType: inferMimeType(file),
      bytes: file.size,
      multipart: file.size > MULTIPART_THRESHOLD_BYTES,
      state: 'queued',
      progress: 0,
      photoId: null,
      photo: null,
      exif: null,
      metadata: blankMetadata(defaultLicense),
      errorTitle: null,
      errorDetail: null,
      attempts: 0,
    }))
    items.value = [...items.value, ...created]

    await Promise.all(
      created.map(async (item) => {
        const exif = await readExifDraft(item.file)
        const target = find(item.id)
        if (!target) return
        target.exif = exif
        target.metadata = {
          ...target.metadata,
          captured_at_local: exif.capturedAtLocal,
          utc_offset_minutes: exif.utcOffsetMinutes,
          location: exif.location,
          equipment: { ...exif.equipment },
        }
      }),
    )

    return created.map((i) => i.id)
  }

  function setMetadata(id: string, patch: Partial<PhotoCompleteRequest>) {
    const item = find(id)
    if (!item) return
    const next = { ...item.metadata, ...patch }
    // Coherencia forzosa: un ND no puede permitir su uso como frame.
    if (next.license && forbidsStackDerivatives(next.license)) next.allow_derivatives_in_stacks = false
    item.metadata = next
  }

  function remove(id: string) {
    cancel(id)
    items.value = items.value.filter((i) => i.id !== id)
  }

  function cancel(id: string) {
    controllers.get(id)?.abort()
    controllers.delete(id)
    const item = find(id)
    if (item && !['done', 'error'].includes(item.state)) item.state = 'cancelled'
  }

  function clearFinished() {
    items.value = items.value.filter((i) => !['done', 'cancelled'].includes(i.state))
  }

  function fail(item: UploadItem, e: unknown) {
    if (e instanceof ApiError) {
      item.errorTitle = e.title
      item.errorDetail = e.detail
    } else if (e instanceof Error) {
      item.errorTitle = e.name
      item.errorDetail = e.message
    } else {
      item.errorTitle = 'unknown_error'
      item.errorDetail = String(e)
    }
    item.state = 'error'
  }

  /** Sube un fichero completo (los 3 pasos), con reintentos exponenciales. */
  async function start(id: string): Promise<boolean> {
    const item = find(id)
    if (!item || item.state === 'done') return false

    const controller = new AbortController()
    controllers.set(id, controller)
    item.errorTitle = null
    item.errorDetail = null
    item.attempts += 1

    try {
      item.state = 'hashing'
      const request = await describe(item.file)

      item.state = 'requesting'
      const presigned = await ticket(request)
      item.photoId = presigned.photo_id
      item.multipart = presigned.multipart !== null && presigned.multipart !== undefined

      item.state = 'transferring'
      await transfer(
        presigned,
        item.file,
        (loaded, total) => {
          item.progress = total > 0 ? Math.min(0.98, loaded / total) : 0
        },
        controller.signal,
      )

      item.state = 'completing'
      item.photo = await complete(presigned.photo_id, item.metadata)
      item.progress = 1
      item.state = 'done'
      return true
    } catch (e) {
      if (item.state === 'cancelled') return false
      const retriable =
        !(e instanceof ApiError) || e.status === 0 || e.status === 429 || e.status >= 500
      if (retriable && item.attempts < MAX_ATTEMPTS) {
        await sleep(RETRY_BASE_MS * 2 ** (item.attempts - 1))
        controllers.delete(id)
        return start(id)
      }
      fail(item, e)
      return false
    } finally {
      controllers.delete(id)
    }
  }

  /** Reinicia el contador de intentos y vuelve a empezar. */
  function retry(id: string) {
    const item = find(id)
    if (!item) return Promise.resolve(false)
    item.attempts = 0
    item.progress = 0
    item.state = 'queued'
    return start(id)
  }

  /** Sube la cola de una en una: no saturamos la red del usuario. */
  async function startAll() {
    for (const item of items.value) {
      if (item.state === 'queued' || item.state === 'error') {
        await start(item.id)
      }
    }
  }

  return {
    items,
    active,
    isBusy,
    totalProgress,
    failed,
    succeeded,
    enqueue,
    setMetadata,
    start,
    startAll,
    retry,
    cancel,
    remove,
    clearFinished,
    find,
  }
})

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
