/* eslint-disable */
/**
 * PLACEHOLDER — NO EDITAR A MANO MÁS ALLÁ DE LO IMPRESCINDIBLE.
 *
 * Este fichero es un *stand-in* escrito a partir de `docs/api.md` y
 * `docs/data-model.md` para que el frontend compile antes de que el backend
 * exponga su OpenAPI. En cuanto `http://localhost:8000/api/v1/openapi.json`
 * esté disponible hay que regenerarlo y **descartar este contenido**:
 *
 *     pnpm run gen:api
 *
 * A partir de ese momento este fichero pasa a ser 100 % generado
 * (openapi-typescript) y cualquier discrepancia se arregla en el contrato,
 * nunca aquí (ver CLAUDE.md, regla dura 7).
 *
 * La forma imita la salida de openapi-typescript v7: `paths`, `components`,
 * `operations`, de modo que el resto del código (`app/types/domain.ts`) pueda
 * seguir importando `components['schemas'][...]` sin cambios tras la primera
 * generación real.
 */

export interface paths {
  '/me': {
    get: operations['getMe']
    patch: operations['updateMe']
  }
  '/users/{id}': { get: operations['getUser'] }
  '/photos': { get: operations['searchPhotos'] }
  '/photos/uploads': { post: operations['createUpload'] }
  '/photos/{id}': {
    get: operations['getPhoto']
    patch: operations['updatePhoto']
    delete: operations['deletePhoto']
  }
  '/photos/{id}/complete': { post: operations['completePhoto'] }
  '/photos/{id}/download': { get: operations['downloadPhoto'] }
  '/photos/similar/{id}': { get: operations['similarPhotos'] }
  '/objects': { get: operations['listObjects'] }
  '/objects/{id}': { get: operations['getObject'] }
  '/objects/{id}/coverage': { get: operations['getObjectCoverage'] }
  '/reconstructions': {
    get: operations['listReconstructions']
    post: operations['createReconstruction']
  }
  '/reconstructions/preview': { post: operations['previewReconstruction'] }
  '/reconstructions/{id}': {
    get: operations['getReconstruction']
    delete: operations['cancelReconstruction']
  }
  '/reconstructions/{id}/events': { get: operations['reconstructionEvents'] }
  '/reconstructions/{id}/inputs': { get: operations['getReconstructionInputs'] }
  '/reconstructions/{id}/result': { get: operations['getReconstructionResult'] }
  '/models': { get: operations['listModels'] }
  '/models/{id}': { get: operations['getModel'] }
  '/licenses': { get: operations['listLicenses'] }
  '/licenses/resolve': { post: operations['resolveLicenses'] }
  /** NO está en docs/api.md — ver el resumen de discrepancias. */
  '/stats': { get: operations['getSiteStats'] }
  '/healthz': { get: operations['healthz'] }
  '/readyz': { get: operations['readyz'] }
}

export interface components {
  schemas: {
    /** RFC 9457 application/problem+json */
    Problem: {
      type: string
      title: string
      status: number
      detail?: string | null
      instance?: string | null
      errors?: components['schemas']['ProblemFieldError'][] | null
    }
    ProblemFieldError: {
      /** JSON pointer o nombre del campo */
      field?: string | null
      message: string
      code?: string | null
    }

    LicenseCode:
      | 'CC0-1.0'
      | 'CC-BY-4.0'
      | 'CC-BY-SA-4.0'
      | 'CC-BY-NC-4.0'
      | 'CC-BY-NC-SA-4.0'
      | 'CC-BY-ND-4.0'
      | 'CC-BY-NC-ND-4.0'
      | 'ARR'
    UserRole: 'member' | 'curator' | 'admin'
    PhotoStatus: 'uploading' | 'processing' | 'ready' | 'failed' | 'quarantined'
    JobStatus: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
    TimeSource: 'exif' | 'gps' | 'user' | 'inferred'
    LocationSource: 'exif_gps' | 'user_pin' | 'named_site' | 'undisclosed'
    LocationPrecision: 'exact' | 'city' | 'country' | 'hidden'
    ObjectType: 'galaxy' | 'nebula' | 'cluster' | 'planet' | 'comet' | 'moon' | 'other'
    ObjectCatalog: 'M' | 'NGC' | 'IC' | 'SH2' | 'solar'
    ModelArchitecture: 'bipnet' | 'burstormer' | 'rbsr' | 'edsr-burst' | 'custom'

    License: {
      code: components['schemas']['LicenseCode']
      name: string
      version: string | null
      url: string | null
      allows_commercial: boolean
      allows_derivatives: boolean
      requires_attribution: boolean
      requires_sharealike: boolean
      /** 0 = más permisiva */
      restrictiveness: number
      spdx_id: string | null
    }

    UserPublic: {
      id: string
      display_name: string
      bio?: string | null
      website_url?: string | null
      photo_count?: number
    }

    Quota: {
      storage_quota_bytes: number
      storage_used_bytes: number
      /** jobs de reconstrucción encolados ahora mismo */
      queued_jobs: number
      max_queued_jobs: number
      jobs_today: number
      max_jobs_per_day: number
    }

    Me: {
      id: string
      email: string
      display_name: string
      bio?: string | null
      website_url?: string | null
      default_license: components['schemas']['LicenseCode']
      role: components['schemas']['UserRole']
      is_active: boolean
      quota: components['schemas']['Quota']
    }

    MePatch: {
      display_name?: string
      bio?: string | null
      website_url?: string | null
      default_license?: components['schemas']['LicenseCode']
    }

    GeoPoint: {
      lat: number
      lon: number
      accuracy_m?: number | null
      elevation_m?: number | null
    }

    Equipment: {
      camera_make?: string | null
      camera_model?: string | null
      sensor_width_mm?: number | null
      sensor_height_mm?: number | null
      pixel_pitch_um?: number | null
      lens_model?: string | null
      focal_length_mm?: number | null
      focal_ratio?: number | null
      aperture_mm?: number | null
      exposure_seconds?: number | null
      iso?: number | null
      is_stacked?: boolean
      sub_frames?: number | null
      telescope_model?: string | null
      mount_model?: string | null
      is_tracked?: boolean | null
      filter_name?: string | null
    }

    Astrometry: {
      is_plate_solved: boolean
      ra_deg?: number | null
      dec_deg?: number | null
      field_radius_deg?: number | null
      pixel_scale_arcsec?: number | null
      orientation_deg?: number | null
      parity?: number | null
      wcs_json?: Record<string, unknown> | null
    }

    QualityMetrics: {
      fwhm_arcsec?: number | null
      star_count?: number | null
      eccentricity?: number | null
      background_adu?: number | null
      snr_estimate?: number | null
      bortle_estimate?: number | null
      moon_illumination?: number | null
      moon_separation_deg?: number | null
      airmass?: number | null
      quality_score?: number | null
    }

    PhotoSummary: {
      id: string
      title?: string | null
      status: components['schemas']['PhotoStatus']
      owner: components['schemas']['UserPublic']
      object_id?: string | null
      object_name?: string | null
      thumb_url?: string | null
      preview_url?: string | null
      width_px?: number | null
      height_px?: number | null
      captured_at_utc?: string | null
      license: components['schemas']['LicenseCode']
      quality_score?: number | null
      focal_length_mm?: number | null
      filter_name?: string | null
      is_plate_solved: boolean
      allow_derivatives_in_stacks: boolean
    }

    Photo: components['schemas']['PhotoSummary'] & {
      description?: string | null
      s3_key_original?: string | null
      original_bytes?: number | null
      mime_type?: string | null
      bit_depth?: number | null
      captured_at_local?: string | null
      utc_offset_minutes?: number | null
      time_source?: components['schemas']['TimeSource'] | null
      location?: components['schemas']['GeoPoint'] | null
      location_source?: components['schemas']['LocationSource'] | null
      location_precision: components['schemas']['LocationPrecision']
      /** etiqueta ya ofuscada por el backend según location_precision */
      location_label?: string | null
      site_id?: string | null
      site_name?: string | null
      equipment: components['schemas']['Equipment']
      astrometry: components['schemas']['Astrometry']
      quality: components['schemas']['QualityMetrics']
      license_locked_at?: string | null
      attribution_name?: string | null
      allow_ai_training: boolean
      view_count?: number
      download_count?: number
      created_at: string
      updated_at: string
    }

    PhotoPatch: {
      title?: string | null
      description?: string | null
      object_id?: string | null
      license?: components['schemas']['LicenseCode']
      attribution_name?: string | null
      allow_ai_training?: boolean
      allow_derivatives_in_stacks?: boolean
      location_precision?: components['schemas']['LocationPrecision']
      equipment?: components['schemas']['Equipment']
    }

    UploadRequest: {
      filename: string
      size_bytes: number
      mime_type: string
      checksum_sha256: string
    }

    /** POST presignado de S3 (no PUT: permite content-length-range) */
    PresignedPost: {
      photo_id: string
      upload_url: string
      fields: Record<string, string>
      expires_at: string
      multipart?: components['schemas']['MultipartUpload'] | null
    }

    MultipartUpload: {
      upload_id: string
      part_size_bytes: number
      part_urls: { part_number: number; url: string }[]
      complete_url?: string | null
    }

    PhotoCompleteRequest: {
      title?: string | null
      description?: string | null
      license: components['schemas']['LicenseCode']
      captured_at_local?: string | null
      utc_offset_minutes?: number | null
      location?: components['schemas']['GeoPoint'] | null
      location_precision: components['schemas']['LocationPrecision']
      object_id?: string | null
      site_id?: string | null
      equipment?: components['schemas']['Equipment']
      attribution_name?: string | null
      allow_ai_training: boolean
      allow_derivatives_in_stacks: boolean
    }

    SkyObject: {
      id: string
      catalog: components['schemas']['ObjectCatalog']
      catalog_number: string
      common_name?: string | null
      common_name_es?: string | null
      object_type: components['schemas']['ObjectType']
      ra_deg?: number | null
      dec_deg?: number | null
      magnitude?: number | null
      size_arcmin?: number | null
      aliases: string[]
      is_ephemeral: boolean
      photo_count: number
      reconstruction_count: number
    }

    /** Una celda del mapa de cobertura: tiempo x latitud x focal */
    CoverageCell: {
      /** inicio del bin temporal, ISO-8601 */
      period_start: string
      /** centro del bin de latitud del observador, grados */
      lat_bin_deg: number
      /** centro del bin de focal, mm */
      focal_bin_mm: number
      photo_count: number
      total_exposure_seconds?: number | null
      best_quality_score?: number | null
    }

    CoverageSite: {
      lat: number
      lon: number
      photo_count: number
      /** precisión con la que se puede pintar el punto */
      precision: components['schemas']['LocationPrecision']
    }

    ObjectCoverage: {
      object_id: string
      cells: components['schemas']['CoverageCell'][]
      sites: components['schemas']['CoverageSite'][]
      /** huecos detectados, ya redactados por el backend */
      gaps: { kind: 'hemisphere' | 'focal' | 'season' | 'filter'; detail: string }[]
      period_bin: 'month' | 'quarter' | 'year'
      lat_bin_size_deg: number
      focal_bins_mm: number[]
    }

    ReconstructionSelector: {
      object?: string | null
      ra?: number | null
      dec?: number | null
      radius?: number | null
      from?: string | null
      to?: string | null
      min_focal?: number | null
      max_focal?: number | null
      filter?: string | null
      license?: components['schemas']['LicenseCode'][] | null
      min_quality?: number | null
      tracked?: boolean | null
      limit?: number | null
    }

    ReconstructionRequest: {
      object_id?: string | null
      photo_ids?: string[] | null
      selector?: components['schemas']['ReconstructionSelector'] | null
      pipeline: string
      params?: Record<string, unknown>
    }

    BlockedPhoto: {
      photo_id: string
      reason: string
      /** código estable para poder traducirlo en el frontend */
      code?: string | null
    }

    ReconstructionPreview: {
      selected: components['schemas']['PhotoSummary'][]
      blocked: components['schemas']['BlockedPhoto'][]
      resulting_license: components['schemas']['LicenseCode']
      estimated_compute_seconds: number
      estimated_cost_usd: number
      estimated_queue_seconds?: number | null
      /** métricas previstas, orientativas */
      projected_metrics?: Record<string, number> | null
      pipeline: string
      uses_learned_model: boolean
    }

    ReconstructionMetrics: {
      fwhm_arcsec?: number | null
      snr_gain_db?: number | null
      effective_pixel_scale?: number | null
      psnr?: number | null
      ssim?: number | null
      [key: string]: number | null | undefined
    }

    Reconstruction: {
      id: string
      requested_by: components['schemas']['UserPublic']
      object_id?: string | null
      object_name?: string | null
      pipeline: string
      pipeline_version: string
      model_id?: string | null
      params: Record<string, unknown>
      status: components['schemas']['JobStatus']
      progress: number
      input_count: number
      preview_url?: string | null
      metrics?: components['schemas']['ReconstructionMetrics'] | null
      license: components['schemas']['LicenseCode']
      error_message?: string | null
      started_at?: string | null
      finished_at?: string | null
      compute_seconds?: number | null
      cost_usd_estimate?: number | null
      created_at: string
      /** true si intervino un modelo aprendido: obliga a etiquetar la salida */
      uses_learned_model: boolean
    }

    ReconstructionInput: {
      photo_id: string
      photo: components['schemas']['PhotoSummary']
      weight: number
      was_rejected: boolean
      rejection_reason?: string | null
      alignment_rms_px?: number | null
      snapshot_license: components['schemas']['LicenseCode']
    }

    ReconstructionResult: {
      id: string
      result_url: string
      preview_url?: string | null
      fits_url?: string | null
      report_url?: string | null
      uncertainty_map_url?: string | null
      attribution_markdown_url?: string | null
      provenance_json_url?: string | null
      /** mejor frame individual, para la comparación honesta */
      best_single_frame?: components['schemas']['PhotoSummary'] | null
      best_single_frame_url?: string | null
      license: components['schemas']['LicenseCode']
      uses_learned_model: boolean
      expires_at?: string | null
    }

    /** Evento SSE de /reconstructions/{id}/events */
    ReconstructionEvent: {
      status: components['schemas']['JobStatus']
      progress: number
      stage?: string | null
      message?: string | null
      metrics?: components['schemas']['ReconstructionMetrics'] | null
      at: string
    }

    ModelCard: {
      id: string
      name: string
      version: string
      architecture: components['schemas']['ModelArchitecture']
      metrics: Record<string, number>
      is_active: boolean
      trained_on_photo_count: number
      card_markdown: string
      respects_ai_optout: boolean
    }

    LicenseResolveRequest: { photo_ids: string[] }

    LicenseResolution: {
      resulting_license: components['schemas']['LicenseCode']
      blocked: components['schemas']['BlockedPhoto'][]
    }

    PagePhotoSummary: {
      items: components['schemas']['PhotoSummary'][]
      next_cursor: string | null
    }
    PageSkyObject: {
      items: components['schemas']['SkyObject'][]
      next_cursor: string | null
    }
    PageReconstruction: {
      items: components['schemas']['Reconstruction'][]
      next_cursor: string | null
    }
    PageReconstructionInput: {
      items: components['schemas']['ReconstructionInput'][]
      next_cursor: string | null
    }

    Health: { status: 'ok' | 'degraded' | 'down'; checks?: Record<string, string> }

    SiteStats: {
      photo_count: number
      object_count: number
      reconstruction_count: number
      contributor_count: number
      total_exposure_seconds: number
    }
  }
}

type Ok<T> = { responses: { 200: { content: { 'application/json': T } } } }

export interface operations {
  getMe: Ok<components['schemas']['Me']>
  updateMe: Ok<components['schemas']['Me']>
  getUser: Ok<components['schemas']['UserPublic']>
  searchPhotos: Ok<components['schemas']['PagePhotoSummary']>
  createUpload: Ok<components['schemas']['PresignedPost']>
  getPhoto: Ok<components['schemas']['Photo']>
  updatePhoto: Ok<components['schemas']['Photo']>
  deletePhoto: { responses: { 204: { content: never } } }
  completePhoto: Ok<components['schemas']['Photo']>
  downloadPhoto: { responses: { 302: { content: never } } }
  similarPhotos: Ok<components['schemas']['PagePhotoSummary']>
  listObjects: Ok<components['schemas']['PageSkyObject']>
  getObject: Ok<components['schemas']['SkyObject']>
  getObjectCoverage: Ok<components['schemas']['ObjectCoverage']>
  listReconstructions: Ok<components['schemas']['PageReconstruction']>
  createReconstruction: Ok<components['schemas']['Reconstruction']>
  previewReconstruction: Ok<components['schemas']['ReconstructionPreview']>
  getReconstruction: Ok<components['schemas']['Reconstruction']>
  cancelReconstruction: { responses: { 204: { content: never } } }
  reconstructionEvents: { responses: { 200: { content: { 'text/event-stream': string } } } }
  getReconstructionInputs: Ok<components['schemas']['PageReconstructionInput']>
  getReconstructionResult: Ok<components['schemas']['ReconstructionResult']>
  listModels: Ok<{ items: components['schemas']['ModelCard'][]; next_cursor: string | null }>
  getModel: Ok<components['schemas']['ModelCard']>
  listLicenses: Ok<components['schemas']['License'][]>
  resolveLicenses: Ok<components['schemas']['LicenseResolution']>
  getSiteStats: Ok<components['schemas']['SiteStats']>
  healthz: Ok<components['schemas']['Health']>
  readyz: Ok<components['schemas']['Health']>
}

export type webhooks = Record<string, never>
export type $defs = Record<string, never>
