/**
 * Transferencia del binario al bucket. **Nunca pasa por el backend**
 * (CLAUDE.md, regla dura 6): POST presignado de S3 o multipart por partes.
 *
 * Se usa XMLHttpRequest en vez de fetch porque es la única forma de tener
 * `upload.onprogress` fiable en todos los navegadores.
 */
import type { MultipartUpload, PresignedPost } from '~/types/domain'

export class UploadError extends Error {
  readonly status: number
  constructor(message: string, status = 0) {
    super(message)
    this.name = 'UploadError'
    this.status = status
  }
}

export type ProgressHandler = (loadedBytes: number, totalBytes: number) => void

interface XhrOptions {
  method: 'POST' | 'PUT'
  url: string
  body: XMLHttpRequestBodyInit
  headers?: Record<string, string>
  onProgress?: ProgressHandler
  signal?: AbortSignal
}

/** Devuelve las cabeceras de respuesta que nos interesan (ETag en multipart). */
export function xhrUpload(options: XhrOptions): Promise<{ status: number; etag: string | null }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open(options.method, options.url, true)
    for (const [k, v] of Object.entries(options.headers ?? {})) xhr.setRequestHeader(k, v)

    const abort = () => xhr.abort()
    options.signal?.addEventListener('abort', abort, { once: true })

    xhr.upload.onprogress = (event: ProgressEvent) => {
      if (event.lengthComputable) options.onProgress?.(event.loaded, event.total)
    }
    xhr.onload = () => {
      options.signal?.removeEventListener('abort', abort)
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve({ status: xhr.status, etag: xhr.getResponseHeader('ETag') })
      } else {
        reject(new UploadError(`S3 respondió ${xhr.status}`, xhr.status))
      }
    }
    xhr.onerror = () => {
      options.signal?.removeEventListener('abort', abort)
      reject(new UploadError('Fallo de red al subir a S3'))
    }
    xhr.onabort = () => {
      options.signal?.removeEventListener('abort', abort)
      reject(new UploadError('Subida cancelada'))
    }

    xhr.send(options.body)
  })
}

/** El `file` va SIEMPRE el último en un POST presignado de S3. */
export function buildPresignedForm(fields: Record<string, string>, file: File): FormData {
  const form = new FormData()
  for (const [k, v] of Object.entries(fields)) form.append(k, v)
  form.append('file', file)
  return form
}

export async function uploadPresignedPost(
  ticket: PresignedPost,
  file: File,
  onProgress?: ProgressHandler,
  signal?: AbortSignal,
): Promise<void> {
  await xhrUpload({
    method: 'POST',
    url: ticket.upload_url,
    body: buildPresignedForm(ticket.fields, file),
    onProgress,
    signal,
  })
}

export interface UploadedPart {
  part_number: number
  etag: string
}

export async function uploadMultipart(
  multipart: MultipartUpload,
  file: File,
  onProgress?: ProgressHandler,
  signal?: AbortSignal,
): Promise<UploadedPart[]> {
  const parts: UploadedPart[] = []
  const total = file.size
  let completed = 0

  for (const part of multipart.part_urls) {
    const start = (part.part_number - 1) * multipart.part_size_bytes
    const end = Math.min(start + multipart.part_size_bytes, total)
    const chunk = file.slice(start, end)

    const { etag } = await xhrUpload({
      method: 'PUT',
      url: part.url,
      body: chunk,
      onProgress: (loaded) => onProgress?.(completed + loaded, total),
      signal,
    })
    completed = end
    onProgress?.(completed, total)
    parts.push({ part_number: part.part_number, etag: (etag ?? '').replaceAll('"', '') })
  }

  return parts
}

/** SHA-256 en hex, para `checksum_sha256` (deduplicación en el backend). */
export async function sha256Hex(file: Blob): Promise<string> {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) throw new UploadError('WebCrypto no disponible: no se puede calcular el checksum')
  const buffer = await file.arrayBuffer()
  const digest = await subtle.digest('SHA-256', buffer)
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

const EXTENSION_MIME: Record<string, string> = {
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  png: 'image/png',
  tif: 'image/tiff',
  tiff: 'image/tiff',
  fit: 'image/fits',
  fits: 'image/fits',
  fts: 'image/fits',
  cr2: 'image/x-canon-cr2',
  cr3: 'image/x-canon-cr3',
  nef: 'image/x-nikon-nef',
  arw: 'image/x-sony-arw',
  dng: 'image/x-adobe-dng',
  raf: 'image/x-fuji-raf',
  orf: 'image/x-olympus-orf',
  rw2: 'image/x-panasonic-rw2',
}

/** Los navegadores dejan `type` vacío para RAW y FITS: lo deducimos. */
export function inferMimeType(file: File): string {
  if (file.type) return file.type
  const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
  return EXTENSION_MIME[ext] ?? 'application/octet-stream'
}

export const ACCEPTED_EXTENSIONS = Object.keys(EXTENSION_MIME).map((e) => `.${e}`)

/** Por encima de esto el backend devuelve `multipart` en vez de un POST. */
export const MULTIPART_THRESHOLD_BYTES = 100 * 1024 * 1024
