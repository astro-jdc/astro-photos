<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { setResponseStatus, useAsyncData, useSeoMeta } from '#app'
import { useApi } from '~/composables/useApi'
import { useObjects, useObjectCoverage } from '~/composables/useObjects'
import { formatCoords, round } from '~/lib/astro'
import type { PagePhotoSummary, PageReconstruction } from '~/types/domain'

const { t } = useI18n()
const route = useRoute()
const api = useApi()
const { get } = useObjects()
const { coverage, pending: coveragePending, load: loadCoverage } = useObjectCoverage()

const id = computed(() => String(route.params.id))

const { data: object, error } = await useAsyncData(`object-${id.value}`, () => get(id.value))

const { data: photos } = await useAsyncData(`object-photos-${id.value}`, () =>
  api.get<PagePhotoSummary>(
    '/photos',
    { object: id.value, limit: 12, sort: 'quality' },
    { anonymous: true },
  ),
)

const { data: reconstructions } = await useAsyncData(`object-recon-${id.value}`, async () => {
  try {
    return await api.get<PageReconstruction>(
      '/reconstructions',
      { object_id: id.value, limit: 6 },
      { anonymous: true },
    )
  } catch {
    return null
  }
})

if (error.value) setResponseStatus(error.value.status === 404 ? 404 : 502)

const name = computed(() => {
  if (!object.value) return ''
  const catalogued = `${object.value.catalog}${object.value.catalog_number}`
  const common = object.value.common_name_es ?? object.value.common_name
  return common ? `${catalogued} — ${common}` : catalogued
})

onMounted(() => void loadCoverage(id.value))

useSeoMeta({ title: () => name.value, description: () => t('coverage.description') })
</script>

<template>
  <div v-if="error" class="surface p-6" role="alert">
    <h1 class="text-xl font-semibold">{{ t('object.notFound') }}</h1>
    <NuxtLink to="/explore" class="btn-secondary mt-4">{{ t('nav.explore') }}</NuxtLink>
  </div>

  <div v-else-if="object" class="grid gap-8">
    <header class="grid gap-2">
      <h1 class="text-2xl font-semibold">{{ name }}</h1>
      <dl class="flex flex-wrap gap-x-6 gap-y-1 text-sm">
        <div class="flex gap-2">
          <dt class="muted">{{ t('object.type') }}:</dt>
          <dd>{{ t(`object.types.${object.object_type}`) }}</dd>
        </div>
        <div v-if="object.magnitude !== null && object.magnitude !== undefined" class="flex gap-2">
          <dt class="muted">{{ t('object.magnitude') }}:</dt>
          <dd class="font-mono">{{ object.magnitude }}</dd>
        </div>
        <div v-if="object.size_arcmin" class="flex gap-2">
          <dt class="muted">{{ t('object.size') }}:</dt>
          <dd class="font-mono">{{ round(object.size_arcmin, 1) }}′</dd>
        </div>
        <div
          v-if="!object.is_ephemeral && object.ra_deg !== null && object.ra_deg !== undefined && object.dec_deg !== null && object.dec_deg !== undefined"
          class="flex gap-2"
        >
          <dt class="muted">{{ t('object.coordinates') }}:</dt>
          <dd class="font-mono">{{ formatCoords(object.ra_deg, object.dec_deg) }}</dd>
        </div>
      </dl>
      <p v-if="object.is_ephemeral" class="muted text-sm">{{ t('object.ephemeral') }}</p>
      <p v-if="(object.aliases ?? []).length > 0" class="muted text-sm">
        {{ t('object.aliases') }}: {{ (object.aliases ?? []).join(', ') }}
      </p>
    </header>

    <section aria-labelledby="object-photos">
      <div class="flex flex-wrap items-center gap-3">
        <h2 id="object-photos" class="text-lg font-semibold">{{ t('object.photos') }}</h2>
        <span class="chip">{{ object.photo_count }}</span>
        <NuxtLink :to="`/explore?object=${object.id}`" class="btn-ghost ml-auto py-1 text-xs">
          {{ t('object.viewAll') }}
        </NuxtLink>
      </div>
      <div v-if="photos && photos.items.length > 0" class="mt-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <PhotoCard v-for="photo in photos.items" :key="photo.id" :photo="photo" />
      </div>
      <p v-else class="muted mt-4 text-sm">{{ t('object.noPhotos') }}</p>
    </section>

    <section aria-labelledby="object-reconstructions">
      <div class="flex flex-wrap items-center gap-3">
        <h2 id="object-reconstructions" class="text-lg font-semibold">
          {{ t('object.reconstructions') }}
        </h2>
        <span class="chip">{{ object.reconstruction_count }}</span>
      </div>
      <ul
        v-if="reconstructions && reconstructions.items.length > 0"
        class="mt-4 grid gap-3 sm:grid-cols-2"
      >
        <li v-for="job in reconstructions.items" :key="job.id" class="surface p-3">
          <NuxtLink :to="`/reconstructions/${job.id}`" class="font-medium hover:underline">
            {{ job.pipeline }}
          </NuxtLink>
          <p class="muted text-xs">
            {{ t(`reconstruction.status.${job.status}`) }} ·
            {{ t('reconstruction.inputCount') }}: {{ job.input_count }}
          </p>
          <div class="mt-2 flex flex-wrap items-center gap-2">
            <LicenseBadge :code="job.license" />
            <AiDisclosure compact :uses-learned-model="(job.model_id !== null && job.model_id !== undefined)" />
          </div>
        </li>
      </ul>
      <p v-else class="muted mt-4 text-sm">{{ t('object.noReconstructions') }}</p>
    </section>

    <CoverageMap :coverage="coverage" :pending="coveragePending" />
  </div>

  <p v-else class="muted" role="status">{{ t('common.loading') }}</p>
</template>
