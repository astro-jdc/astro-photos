<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useAsyncData, useSeoMeta } from '#app'
import { useApi } from '~/composables/useApi'
import { formatExposure } from '~/lib/astro'
import type { SiteStats } from '~/types/domain'

const { t } = useI18n()
const api = useApi()

const { data: stats } = await useAsyncData<SiteStats | null>('site-stats', async () => {
  try {
    return await api.get<SiteStats>('/stats', undefined, { anonymous: true })
  } catch {
    // El contador es un adorno: si no está, la página sigue siendo útil.
    return null
  }
})

useSeoMeta({
  title: () => t('home.title'),
  description: () => t('home.subtitle'),
  ogTitle: () => t('home.title'),
  ogDescription: () => t('home.subtitle'),
})
</script>

<template>
  <div class="grid gap-12">
    <section class="grid gap-4">
      <h1 class="max-w-4xl text-3xl font-semibold leading-tight sm:text-4xl">
        {{ t('home.title') }}
      </h1>
      <p class="muted max-w-3xl text-lg">{{ t('home.subtitle') }}</p>
      <div class="flex flex-wrap gap-3">
        <NuxtLink to="/explore" class="btn-primary">{{ t('home.ctaExplore') }}</NuxtLink>
        <NuxtLink to="/upload" class="btn-secondary">{{ t('home.ctaUpload') }}</NuxtLink>
      </div>
    </section>

    <section aria-labelledby="stats-title" class="surface p-6">
      <h2 id="stats-title" class="text-lg font-semibold">{{ t('home.stats.title') }}</h2>
      <dl
        v-if="stats"
        class="mt-4 grid grid-cols-2 gap-6 sm:grid-cols-3 lg:grid-cols-5"
        data-testid="site-stats"
      >
        <div>
          <dd class="text-3xl font-semibold tabular-nums">{{ stats.photo_count }}</dd>
          <dt class="muted text-sm">{{ t('home.stats.photos') }}</dt>
        </div>
        <div>
          <dd class="text-3xl font-semibold tabular-nums">{{ stats.object_count }}</dd>
          <dt class="muted text-sm">{{ t('home.stats.objects') }}</dt>
        </div>
        <div>
          <dd class="text-3xl font-semibold tabular-nums">{{ stats.reconstruction_count }}</dd>
          <dt class="muted text-sm">{{ t('home.stats.reconstructions') }}</dt>
        </div>
        <div>
          <dd class="text-3xl font-semibold tabular-nums">{{ stats.contributor_count }}</dd>
          <dt class="muted text-sm">{{ t('home.stats.contributors') }}</dt>
        </div>
        <div>
          <dd class="text-3xl font-semibold tabular-nums">
            {{ formatExposure(stats.total_exposure_seconds) }}
          </dd>
          <dt class="muted text-sm">{{ t('home.stats.exposure') }}</dt>
        </div>
      </dl>
      <p v-else class="muted mt-4 text-sm">{{ t('home.stats.unavailable') }}</p>
    </section>

    <section aria-labelledby="gains-title" class="grid gap-4">
      <h2 id="gains-title" class="text-2xl font-semibold">{{ t('home.gains.title') }}</h2>
      <p class="max-w-4xl leading-relaxed">{{ t('home.gains.p1') }}</p>
      <p class="max-w-4xl leading-relaxed">{{ t('home.gains.p2') }}</p>
    </section>

    <section aria-labelledby="limits-title" class="surface p-6">
      <h2 id="limits-title" class="text-2xl font-semibold">{{ t('home.limits.title') }}</h2>
      <ul class="mt-4 grid max-w-4xl list-inside list-disc gap-2">
        <li>{{ t('home.limits.aperture') }}</li>
        <li>{{ t('home.limits.diffraction') }}</li>
        <li>{{ t('home.limits.parallax') }}</li>
        <li>{{ t('home.limits.generative') }}</li>
      </ul>
    </section>

    <section aria-labelledby="how-title" class="grid gap-4">
      <h2 id="how-title" class="text-2xl font-semibold">{{ t('home.how.title') }}</h2>
      <ol class="grid gap-4 md:grid-cols-3">
        <li class="surface p-4">
          <h3 class="font-medium">{{ t('home.how.step1Title') }}</h3>
          <p class="muted mt-2 text-sm">{{ t('home.how.step1Body') }}</p>
        </li>
        <li class="surface p-4">
          <h3 class="font-medium">{{ t('home.how.step2Title') }}</h3>
          <p class="muted mt-2 text-sm">{{ t('home.how.step2Body') }}</p>
        </li>
        <li class="surface p-4">
          <h3 class="font-medium">{{ t('home.how.step3Title') }}</h3>
          <p class="muted mt-2 text-sm">{{ t('home.how.step3Body') }}</p>
        </li>
      </ol>
    </section>
  </div>
</template>
