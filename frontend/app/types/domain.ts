/**
 * Alias de conveniencia sobre los tipos generados del OpenAPI.
 *
 * Regla dura 7 de CLAUDE.md: ningún tipo de red se escribe a mano. Aquí solo
 * hay *alias* — cero declaraciones estructurales nuevas — de forma que
 * `pnpm run gen:api` pueda sobrescribir `api.gen.ts` sin romper nada.
 *
 * Los nombres de la izquierda son los que usa la interfaz; los de la derecha,
 * los que FastAPI deriva de las clases Pydantic del backend (`...In` para lo
 * que entra, `...Out` para lo que sale). Este fichero es el único punto donde
 * se traducen: si el backend renombra un schema, se arregla aquí y el resto
 * del frontend no se entera.
 */
import type { components } from './api.gen'

type S = components['schemas']

export type Problem = S['ProblemDetail']
export type ProblemFieldError = S['ProblemError']

export type LicenseCode = S['LicenseCode']
/** Una entrada del catálogo de `GET /licenses` (flags de la licencia). */
export type License = S['LicenseInfoOut']
export type LicenseCatalog = S['LicenseCatalogOut']
/** El bloque `license` que cuelga de una foto (código + consentimientos). */
export type PhotoLicense = S['LicenseOut']
export type LicenseResolution = S['LicenseResolveOut']
export type LicenseResolveRequest = S['LicenseResolveIn']
export type BlockedPhoto = S['BlockedPhotoOut']
export type BlockReason = S['BlockReason']

export type UserRole = S['Role']
export type UserPublic = S['PublicUserOut']
export type Me = S['MeOut']
export type MePatch = S['UserUpdateIn']
export type Quota = S['QuotaOut']

export type PhotoStatus = S['PhotoStatus']
export type Photo = S['PhotoOut']
export type PhotoSummary = S['PhotoSummaryOut']
export type PhotoPatch = S['PhotoUpdateIn']
export type PhotoCompleteRequest = S['PhotoCompleteIn']
/** Óptica declarada por el usuario (entrada). */
export type Equipment = S['EquipmentIn']
/** Óptica publicada, con los campos derivados (apertura, límite de difracción). */
export type Optics = S['OpticsOut']
export type Astrometry = S['AstrometryOut']
export type QualityMetrics = S['QualityOut']
export type GeoPoint = S['LocationOut']
export type GeoPointIn = S['LocationIn']
export type TimeSource = S['TimeSource']
export type LocationSource = S['LocationSource']
export type LocationPrecision = S['LocationPrecision']

export type UploadRequest = S['UploadRequestIn']
export type UploadTicket = S['UploadTicketOut']
export type PresignedPost = S['PresignedUploadOut']
export type MultipartUpload = S['MultipartUploadOut']
export type MultipartComplete = S['MultipartCompleteIn']
export type MultipartPart = S['MultipartPartOut']
export type Download = S['DownloadOut']

export type SkyObject = S['ObjectOut']
export type ObjectType = S['ObjectType']
export type ObjectCatalog = S['ObjectCatalog']
export type ObjectCoverage = S['CoverageOut']
export type CoverageCell = S['CoverageCell']
export type CoverageSite = S['CoverageSite']
export type CoverageGap = S['CoverageGap']

export type JobStatus = S['JobStatus']
export type Reconstruction = S['ReconstructionOut']
export type ReconstructionRequest = S['ReconstructionCreateIn']
export type ReconstructionPreview = S['ReconstructionPlanOut']
export type ReconstructionInput = S['ReconstructionInputOut']
export type ReconstructionResult = S['ReconstructionResultOut']
export type SelectedFrame = S['SelectedFrameOut']
export type RejectedFrame = S['RejectedFrameOut']
export type RejectionReason = S['RejectionReason']
export type BestSingleFrame = S['BestSingleFrameOut']

export type ModelSummary = S['ModelOut']
export type ModelCard = S['ModelDetailOut']
export type ModelArchitecture = S['ModelArchitecture']
export type SiteStats = S['StatsOut']
export type Health = S['HealthOut']
export type Readiness = S['ReadinessOut']

export type PhotoSearchQuery = S['PhotoSearchQuery']
export type SortOrder = S['SortOrder']
export type UsableFor = S['UsableFor']

export type Page<T> = { items: T[]; next_cursor: string | null }
export type PagePhotoSummary = S['Page_PhotoSummaryOut_']
export type PageSkyObject = S['Page_ObjectOut_']
export type PageReconstruction = S['Page_ReconstructionOut_']
export type PageModel = S['Page_ModelOut_']

/**
 * Evento del SSE de `GET /reconstructions/{id}/events`.
 *
 * FastAPI no describe el cuerpo de un `text/event-stream` en el OpenAPI, así
 * que no hay schema generado del que tirar. Se declara a partir de los campos
 * que `docs/api.md` fija para el evento, reutilizando los tipos generados en
 * todo lo que sí existe (`status`, `metrics`).
 */
export interface ReconstructionEvent {
  status: JobStatus
  progress: number
  stage?: string | null
  message?: string | null
  metrics?: Reconstruction['metrics']
  at: string
}

export type ReconstructionMetrics = Reconstruction['metrics']
export type ReconstructionSelector = NonNullable<ReconstructionRequest['selector']>
