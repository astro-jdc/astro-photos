import { useApi } from './useApi'
import {
  inferMimeType,
  sha256Hex,
  uploadMultipart,
  uploadPresignedPost,
  type ProgressHandler,
  type UploadedPart,
} from '~/lib/upload'
import type { Photo, PhotoCompleteRequest, UploadRequest, UploadTicket } from '~/types/domain'

/**
 * Los tres pasos de docs/api.md:
 *   1. `POST /photos/uploads`  → ticket presignado (o multipart)
 *   2. subida directa a S3     → el binario nunca toca el backend
 *   3. `POST /photos/{id}/complete` → metadata + encolado del pipeline
 */
export function useUpload() {
  const api = useApi()

  async function describe(file: File): Promise<UploadRequest> {
    return {
      filename: file.name,
      size_bytes: file.size,
      mime_type: inferMimeType(file),
      checksum_sha256: await sha256Hex(file),
    }
  }

  function ticket(request: UploadRequest) {
    return api.post<UploadTicket>('/photos/uploads', request)
  }

  /**
   * `UploadTicketOut` trae exactamente uno de `presigned_post` o `multipart`
   * (lo garantiza el backend); aquí se elige el camino según cuál viene.
   */
  async function transfer(
    ticket: UploadTicket,
    file: File,
    onProgress?: ProgressHandler,
    signal?: AbortSignal,
  ): Promise<UploadedPart[] | null> {
    if (ticket.multipart) {
      return uploadMultipart(ticket.multipart, file, onProgress, signal)
    }
    if (!ticket.presigned_post) {
      throw new Error('El ticket de subida no trae ni presigned_post ni multipart.')
    }
    await uploadPresignedPost(ticket.presigned_post, file, onProgress, signal)
    return null
  }

  function complete(photoId: string, body: PhotoCompleteRequest) {
    return api.post<Photo>(`/photos/${photoId}/complete`, body)
  }

  return { describe, ticket, transfer, complete }
}
