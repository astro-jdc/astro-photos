/**
 * Alias de conveniencia sobre los tipos generados del OpenAPI.
 *
 * Regla dura 7 de CLAUDE.md: ningún tipo de red se escribe a mano. Aquí solo
 * hay *alias* — cero declaraciones estructurales nuevas — de forma que
 * `pnpm run gen:api` pueda sobrescribir `api.gen.ts` sin romper nada.
 */
import type { components } from './api.gen'

type S = components['schemas']

export type Problem = S['Problem']
export type ProblemFieldError = S['ProblemFieldError']

export type LicenseCode = S['LicenseCode']
export type License = S['License']
export type LicenseResolution = S['LicenseResolution']
export type LicenseResolveRequest = S['LicenseResolveRequest']
export type BlockedPhoto = S['BlockedPhoto']

export type UserRole = S['UserRole']
export type UserPublic = S['UserPublic']
export type Me = S['Me']
export type MePatch = S['MePatch']
export type Quota = S['Quota']

export type PhotoStatus = S['PhotoStatus']
export type Photo = S['Photo']
export type PhotoSummary = S['PhotoSummary']
export type PhotoPatch = S['PhotoPatch']
export type PhotoCompleteRequest = S['PhotoCompleteRequest']
export type Equipment = S['Equipment']
export type Astrometry = S['Astrometry']
export type QualityMetrics = S['QualityMetrics']
export type GeoPoint = S['GeoPoint']
export type TimeSource = S['TimeSource']
export type LocationSource = S['LocationSource']
export type LocationPrecision = S['LocationPrecision']

export type UploadRequest = S['UploadRequest']
export type PresignedPost = S['PresignedPost']
export type MultipartUpload = S['MultipartUpload']

export type SkyObject = S['SkyObject']
export type ObjectType = S['ObjectType']
export type ObjectCatalog = S['ObjectCatalog']
export type ObjectCoverage = S['ObjectCoverage']
export type CoverageCell = S['CoverageCell']
export type CoverageSite = S['CoverageSite']

export type JobStatus = S['JobStatus']
export type Reconstruction = S['Reconstruction']
export type ReconstructionRequest = S['ReconstructionRequest']
export type ReconstructionPreview = S['ReconstructionPreview']
export type ReconstructionSelector = S['ReconstructionSelector']
export type ReconstructionInput = S['ReconstructionInput']
export type ReconstructionResult = S['ReconstructionResult']
export type ReconstructionEvent = S['ReconstructionEvent']
export type ReconstructionMetrics = S['ReconstructionMetrics']

export type ModelCard = S['ModelCard']
export type SiteStats = S['SiteStats']
export type Health = S['Health']

export type Page<T> = { items: T[]; next_cursor: string | null }
export type PagePhotoSummary = S['PagePhotoSummary']
export type PageSkyObject = S['PageSkyObject']
export type PageReconstruction = S['PageReconstruction']
export type PageReconstructionInput = S['PageReconstructionInput']
