import { useApi } from './useApi'
import {
  inferMimeType,
  sha256Hex,
  uploadMultipart,
  uploadPresignedPost,
  type ProgressHandler,
  type UploadedPart,
} from '~/lib/upload'
import type { Photo, PhotoCompleteRequest, PresignedPost, UploadRequest } from '~/types/domain'

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
    return api.post<PresignedPost>('/photos/uploads', request)
  }

  async function transfer(
    presigned: PresignedPost,
    file: File,
    onProgress?: ProgressHandler,
    signal?: AbortSignal,
  ): Promise<UploadedPart[] | null> {
    if (presigned.multipart) {
      return uploadMultipart(presigned.multipart, file, onProgress, signal)
    }
    await uploadPresignedPost(presigned, file, onProgress, signal)
    return null
  }

  function complete(photoId: string, body: PhotoCompleteRequest) {
    return api.post<Photo>(`/photos/${photoId}/complete`, body)
  }

  return { describe, ticket, transfer, complete }
}
