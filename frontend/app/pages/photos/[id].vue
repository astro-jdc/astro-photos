<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { setResponseStatus, useAsyncData, useSeoMeta } from '#app'
import { usePhoto } from '~/composables/usePhotos'
import { useBuilderStore } from '~/stores/builder'
import { licenseFacts } from '~/lib/licensing'
import {
  diffractionLimitArcsec,
  formatAngle,
  formatBytes,
  formatDec,
  formatExposure,
  formatRa,
  formatUtcOffset,
  round,
} from '~/lib/astro'
import type { PhotoSummary } from '~/types/domain'

const { t } = useI18n()
const route = useRoute()
const builder = useBuilderStore()
const { fetch, similar, downloadUrl } = usePhoto()

const id = computed(() => String(route.params.id))

const { data: photo, error } = await useAsyncData(`photo-${id.value}`, () => fetch(id.value))
const { data: similarPage } = await useAsyncData(`photo-similar-${id.value}`, async () => {
  try {
    return await similar(id.value, 6)
  } catch {
    return null
  }
})

// Una ficha inexistente debe responder 404, no un 200 con texto de error:
// estas rutas son públicas, indexables y se sirven por ISR.
if (error.value) setResponseStatus(error.value.status === 404 ? 404 : 502)

const title = computed(() => photo.value?.title || t('photo.untitled'))
const facts = computed(() => (photo.value ? licenseFacts(photo.value.license) : null))

const summary = computed<PhotoSummary | null>(() => photo.value ?? null)
const inBuilder = computed(() => (photo.value ? builder.has(photo.value.id) : false))

const diffraction = computed(() => {
  const aperture = photo.value?.equipment.aperture_mm
  if (!aperture) return null
  const value = diffractionLimitArcsec(aperture)
  return Number.isFinite(value) ? `${round(value, 2)}″` : null
})

useSeoMeta({
  title: () => title.value,
  description: () => photo.value?.description ?? t('common.tagline'),
  ogImage: () => photo.value?.preview_url ?? undefined,
})
</script>

<template>
  <div v-if="error" class="surface p-6" role="alert">
    <h1 class="text-xl font-semibold">{{ t('photo.notFound') }}</h1>
    <NuxtLink to="/explore" class="btn-secondary mt-4">{{ t('nav.explore') }}</NuxtLink>
  </div>

  <div v-else-if="photo" class="grid gap-6">
    <header class="grid gap-2">
      <h1 class="text-2xl font-semibold">{{ title }}</h1>
      <p class="muted text-sm">
        {{ t('photo.by', { name: photo.attribution_name || photo.owner.display_name }) }}
        <template v-if="photo.object_name">
          ·
          <NuxtLink v-if="photo.object_id" :to="`/objects/${photo.object_id}`" class="underline">
            {{ photo.object_name }}
          </NuxtLink>
        </template>
      </p>
      <div class="flex flex-wrap items-center gap-2">
        <QualityBadge :score="photo.quality_score" />
        <LicenseBadge :code="photo.license" show-link />
        <span v-if="photo.status !== 'ready'" class="chip border-amber-500/60 text-amber-300">
          {{ t(`upload.state.${photo.status === 'processing' ? 'completing' : 'queued'}`) }}
        </span>
      </div>
      <p v-if="photo.status === 'quarantined'" class="text-sm text-rose-300" role="alert">
        {{ t('photo.quarantined') }}
      </p>
      <p v-else-if="photo.status !== 'ready'" class="muted text-sm">{{ t('photo.notReady') }}</p>
    </header>

    <AstroViewer
      v-if="photo.preview_url"
      :src="photo.preview_url"
      :fallback-src="photo.thumb_url"
      :title="title"
      :astrometry="photo.astrometry"
    />

    <div class="flex flex-wrap gap-3">
      <a
        :href="downloadUrl(photo.id)"
        class="btn-primary"
        :aria-disabled="!facts?.allowsDerivatives && photo.license === 'ARR'"
      >
        {{ t('photo.download') }}
      </a>
      <button
        type="button"
        :class="inBuilder ? 'btn-secondary' : 'btn-secondary'"
        :disabled="!photo.allow_derivatives_in_stacks"
        :title="photo.allow_derivatives_in_stacks ? undefined : t('photo.cannotStack')"
        @click="summary && builder.toggle(summary)"
      >
        {{ inBuilder ? t('photo.removeFromBuilder') : t('photo.addToBuilder') }}
      </button>
    </div>
    <p class="muted text-sm">{{ t('photo.downloadHint') }}</p>

    <section class="surface p-4">
      <h2 class="text-lg font-semibold">{{ t('photo.sections.license') }}</h2>
      <p class="mt-2 text-sm">{{ facts ? t(facts.descriptionKey) : '' }}</p>
      <dl class="mt-3 grid gap-1 text-sm sm:grid-cols-2">
        <div class="flex gap-2">
          <dt class="muted">{{ t('license.picker.attribution') }}:</dt>
          <dd>{{ photo.attribution_name || photo.owner.display_name }}</dd>
        </div>
        <div class="flex gap-2">
          <dt class="muted">{{ t('license.picker.allowAiTraining') }}:</dt>
          <dd>{{ photo.allow_ai_training ? t('common.yes') : t('common.no') }}</dd>
        </div>
        <div class="flex gap-2">
          <dt class="muted">{{ t('license.picker.allowDerivatives') }}:</dt>
          <dd>{{ photo.allow_derivatives_in_stacks ? t('common.yes') : t('common.no') }}</dd>
        </div>
        <div v-if="photo.license_locked_at" class="flex gap-2">
          <dt class="muted">{{ t('license.picker.default') }}:</dt>
          <dd>{{ photo.license_locked_at }}</dd>
        </div>
      </dl>
    </section>

    <div class="grid gap-4 md:grid-cols-2">
      <section class="surface p-4">
        <h2 class="text-lg font-semibold">{{ t('photo.sections.time') }}</h2>
        <dl class="mt-3 grid gap-1 text-sm">
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.capturedUtc') }}</dt>
            <dd class="font-mono">{{ photo.captured_at_utc ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.capturedLocal') }}</dt>
            <dd class="font-mono">{{ photo.captured_at_local ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.utcOffset') }}</dt>
            <dd class="font-mono">{{ formatUtcOffset(photo.utc_offset_minutes) }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.timeSource') }}</dt>
            <dd>{{ photo.time_source ? t(`photo.timeSource.${photo.time_source}`) : t('common.unknown') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.exposure') }}</dt>
            <dd>{{ formatExposure(photo.equipment.exposure_seconds) }}</dd>
          </div>
        </dl>
      </section>

      <section class="surface p-4">
        <h2 class="text-lg font-semibold">{{ t('photo.sections.place') }}</h2>
        <dl class="mt-3 grid gap-1 text-sm">
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.locationPrecision') }}</dt>
            <dd>{{ t(`photo.precision.${photo.location_precision}`) }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.location') }}</dt>
            <dd class="font-mono">
              {{
                photo.location_label ??
                (photo.location
                  ? `${round(photo.location.lat, 3)}, ${round(photo.location.lon, 3)}`
                  : t('common.notAvailable'))
              }}
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.elevation') }}</dt>
            <dd>
              {{ photo.location?.elevation_m ? `${round(photo.location.elevation_m, 0)} m` : t('common.notAvailable') }}
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.site') }}</dt>
            <dd>{{ photo.site_name ?? t('common.notAvailable') }}</dd>
          </div>
        </dl>
        <p class="field-help">{{ t(`photo.precisionHelp.${photo.location_precision}`) }}</p>
      </section>

      <section class="surface p-4">
        <h2 class="text-lg font-semibold">{{ t('photo.sections.optics') }}</h2>
        <dl class="mt-3 grid gap-1 text-sm">
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.camera') }}</dt>
            <dd>
              {{ [photo.equipment.camera_make, photo.equipment.camera_model].filter(Boolean).join(' ') || t('common.notAvailable') }}
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.lens') }}</dt>
            <dd>{{ photo.equipment.lens_model ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.telescope') }}</dt>
            <dd>{{ photo.equipment.telescope_model ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.mount') }}</dt>
            <dd>{{ photo.equipment.mount_model ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.focalLength') }}</dt>
            <dd>{{ photo.equipment.focal_length_mm ? `${photo.equipment.focal_length_mm} mm` : t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.focalRatio') }}</dt>
            <dd>{{ photo.equipment.focal_ratio ? `f/${photo.equipment.focal_ratio}` : t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.aperture') }}</dt>
            <dd>{{ photo.equipment.aperture_mm ? `${round(photo.equipment.aperture_mm, 1)} mm` : t('common.notAvailable') }}</dd>
          </div>
          <div v-if="diffraction" class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.diffractionLimit') }}</dt>
            <dd class="font-mono">{{ diffraction }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.filter') }}</dt>
            <dd>{{ photo.equipment.filter_name ?? t('common.none') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.iso') }}</dt>
            <dd>{{ photo.equipment.iso ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.stacked') }}</dt>
            <dd>
              {{ photo.equipment.is_stacked ? t('common.yes') : t('common.no') }}
              <template v-if="photo.equipment.sub_frames">
                ({{ photo.equipment.sub_frames }})
              </template>
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.tracked') }}</dt>
            <dd>{{ photo.equipment.is_tracked ? t('common.yes') : t('common.no') }}</dd>
          </div>
        </dl>
      </section>

      <section class="surface p-4">
        <h2 class="text-lg font-semibold">{{ t('photo.sections.astrometry') }}</h2>
        <p v-if="!photo.astrometry.is_plate_solved" class="muted mt-2 text-sm">
          {{ t('photo.fields.notPlateSolved') }}
        </p>
        <dl v-else class="mt-3 grid gap-1 text-sm">
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.ra') }}</dt>
            <dd class="font-mono">
              {{ photo.astrometry.ra_deg !== null && photo.astrometry.ra_deg !== undefined ? formatRa(photo.astrometry.ra_deg) : t('common.notAvailable') }}
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.dec') }}</dt>
            <dd class="font-mono">
              {{ photo.astrometry.dec_deg !== null && photo.astrometry.dec_deg !== undefined ? formatDec(photo.astrometry.dec_deg) : t('common.notAvailable') }}
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.pixelScale') }}</dt>
            <dd class="font-mono">
              {{ photo.astrometry.pixel_scale_arcsec ? `${round(photo.astrometry.pixel_scale_arcsec, 3)} ″/px` : t('common.notAvailable') }}
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.fieldRadius') }}</dt>
            <dd class="font-mono">
              {{ photo.astrometry.field_radius_deg ? formatAngle(photo.astrometry.field_radius_deg) : t('common.notAvailable') }}
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.orientation') }}</dt>
            <dd class="font-mono">
              {{ photo.astrometry.orientation_deg !== null && photo.astrometry.orientation_deg !== undefined ? `${round(photo.astrometry.orientation_deg, 2)}°` : t('common.notAvailable') }}
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.parity') }}</dt>
            <dd class="font-mono">{{ photo.astrometry.parity ?? t('common.notAvailable') }}</dd>
          </div>
        </dl>
      </section>

      <section class="surface p-4">
        <h2 class="text-lg font-semibold">{{ t('photo.sections.quality') }}</h2>
        <dl class="mt-3 grid gap-1 text-sm">
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.fwhm') }}</dt>
            <dd class="font-mono">
              {{ photo.quality.fwhm_arcsec ? `${round(photo.quality.fwhm_arcsec, 2)}″` : t('common.notAvailable') }}
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.starCount') }}</dt>
            <dd class="font-mono">{{ photo.quality.star_count ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.eccentricity') }}</dt>
            <dd class="font-mono">{{ photo.quality.eccentricity ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.snr') }}</dt>
            <dd class="font-mono">{{ photo.quality.snr_estimate ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.bortle') }}</dt>
            <dd class="font-mono">{{ photo.quality.bortle_estimate ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.moonIllumination') }}</dt>
            <dd class="font-mono">{{ photo.quality.moon_illumination ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.moonSeparation') }}</dt>
            <dd class="font-mono">
              {{ photo.quality.moon_separation_deg ? `${round(photo.quality.moon_separation_deg, 1)}°` : t('common.notAvailable') }}
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.airmass') }}</dt>
            <dd class="font-mono">{{ photo.quality.airmass ?? t('common.notAvailable') }}</dd>
          </div>
        </dl>
      </section>

      <section class="surface p-4">
        <h2 class="text-lg font-semibold">{{ t('photo.sections.file') }}</h2>
        <dl class="mt-3 grid gap-1 text-sm">
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.dimensions') }}</dt>
            <dd class="font-mono">
              {{ photo.width_px && photo.height_px ? `${photo.width_px} × ${photo.height_px}` : t('common.notAvailable') }}
            </dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.bitDepth') }}</dt>
            <dd class="font-mono">{{ photo.bit_depth ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.fileSize') }}</dt>
            <dd class="font-mono">{{ formatBytes(photo.original_bytes) }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.mimeType') }}</dt>
            <dd class="font-mono">{{ photo.mime_type ?? t('common.notAvailable') }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="muted">{{ t('photo.fields.downloads') }}</dt>
            <dd class="font-mono">{{ photo.download_count ?? 0 }}</dd>
          </div>
        </dl>
      </section>
    </div>

    <section v-if="similarPage && similarPage.items.length > 0">
      <h2 class="text-lg font-semibold">{{ t('photo.similar') }}</h2>
      <div class="mt-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <PhotoCard v-for="item in similarPage.items" :key="item.id" :photo="item" />
      </div>
    </section>
  </div>

  <p v-else class="muted" role="status">{{ t('common.loading') }}</p>
</template>
