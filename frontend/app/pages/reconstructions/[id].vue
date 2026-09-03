<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { useSeoMeta } from '#app'
import { useReconstruction } from '~/composables/useReconstruction'
import { formatExposure, round } from '~/lib/astro'

const { t } = useI18n()
const route = useRoute()

const {
  job,
  inputs,
  result,
  lastEvent,
  progress,
  streaming,
  error,
  load,
  loadInputs,
  loadResult,
  connect,
  cancel,
} = useReconstruction()

const id = computed(() => String(route.params.id))
const sliderPosition = ref(50)

const finished = computed(() => job.value?.status === 'succeeded')
const running = computed(() => job.value?.status === 'queued' || job.value?.status === 'running')

const metricEntries = computed(() => {
  const metrics = job.value?.metrics
  if (!metrics) return []
  return Object.entries(metrics).filter(
    (entry): entry is [string, number] => typeof entry[1] === 'number',
  )
})

function metricLabel(key: string): string {
  const translated = t(`reconstruction.metricNames.${key}`)
  return translated.startsWith('reconstruction.metricNames.') ? key : translated
}

onMounted(async () => {
  await load(id.value)
  await loadInputs(id.value)
  if (running.value) {
    connect(id.value, async () => {
      await load(id.value)
      await loadInputs(id.value)
      await loadResult(id.value)
    })
  }
})

useSeoMeta({ title: () => t('reconstruction.title') })
</script>

<template>
  <div v-if="error && !job" class="surface p-6" role="alert">
    <h1 class="text-xl font-semibold">{{ t('reconstruction.notFound') }}</h1>
    <NuxtLink to="/reconstructions" class="btn-secondary mt-4">
      {{ t('reconstruction.listTitle') }}
    </NuxtLink>
  </div>

  <div v-else-if="job" class="grid gap-6">
    <header class="grid gap-2">
      <h1 class="text-2xl font-semibold">
        {{ job.object_name ?? t('reconstruction.title') }}
      </h1>
      <div class="flex flex-wrap items-center gap-2 text-sm">
        <span class="chip">{{ t(`reconstruction.status.${job.status}`) }}</span>
        <span class="chip font-mono">{{ job.pipeline }}</span>
        <LicenseBadge :code="job.license" />
        <span v-if="streaming" class="chip border-emerald-500/60 text-emerald-300">
          {{ t('reconstruction.live') }}
        </span>
      </div>
    </header>

    <section v-if="running" class="surface p-4" aria-labelledby="progress-title">
      <h2 id="progress-title" class="text-lg font-semibold">
        {{ t('reconstruction.progress') }}
      </h2>
      <div
        class="mt-3 h-3 w-full overflow-hidden rounded bg-night-800"
        role="progressbar"
        :aria-valuenow="Math.round(progress * 100)"
        aria-valuemin="0"
        aria-valuemax="100"
        :aria-label="t('reconstruction.progress')"
      >
        <div
          class="h-full bg-sky-500 transition-[width]"
          :style="{ width: `${Math.round(progress * 100)}%` }"
        />
      </div>
      <p class="muted mt-2 text-sm" aria-live="polite">
        {{ Math.round(progress * 100) }}%
        <template v-if="lastEvent?.stage">
          · {{ t('reconstruction.stage') }}: {{ lastEvent.stage }}
        </template>
        <template v-else> · {{ t('reconstruction.waiting') }}</template>
      </p>
      <p v-if="!streaming" class="muted mt-1 text-sm">{{ t('reconstruction.notLive') }}</p>
      <button type="button" class="btn-secondary mt-3" @click="cancel(id)">
        {{ t('reconstruction.cancel') }}
      </button>
    </section>

    <p
      v-else-if="job.status === 'failed'"
      class="surface border-rose-500/50 bg-rose-500/10 p-4 text-sm"
      role="alert"
    >
      {{ t('reconstruction.failedWith', { message: job.error_message ?? t('errors.generic') }) }}
    </p>

    <AiDisclosure
      :uses-learned-model="job.uses_learned_model"
      :uncertainty-url="result?.uncertainty_map_url ?? null"
      comparison-href="#comparison"
    />

    <section v-if="finished && result" id="comparison" class="surface p-4">
      <h2 class="text-lg font-semibold">{{ t('reconstruction.compare') }}</h2>
      <p class="muted mt-1 text-sm">{{ t('reconstruction.compareHint') }}</p>

      <div
        v-if="result.best_single_frame_url && result.preview_url"
        class="relative mt-4 aspect-video w-full overflow-hidden rounded-lg bg-black"
      >
        <img
          :src="result.best_single_frame_url"
          :alt="t('reconstruction.compareBest')"
          class="absolute inset-0 h-full w-full object-contain"
        />
        <div
          class="absolute inset-y-0 left-0 overflow-hidden"
          :style="{ width: `${sliderPosition}%` }"
        >
          <img
            :src="result.preview_url"
            :alt="t('reconstruction.compareResult')"
            class="h-full w-full object-contain"
            :style="{ width: `${(100 / Math.max(sliderPosition, 1)) * 100}%`, maxWidth: 'none' }"
          />
        </div>
        <div
          class="pointer-events-none absolute inset-y-0 w-0.5 bg-sky-400"
          :style="{ left: `${sliderPosition}%` }"
        />
      </div>
      <p v-else class="muted mt-4 text-sm">{{ t('reconstruction.compareUnavailable') }}</p>

      <label class="field-label mt-3" for="compare-slider">
        {{ t('reconstruction.sliderLabel') }}
      </label>
      <input
        id="compare-slider"
        v-model.number="sliderPosition"
        type="range"
        min="0"
        max="100"
        step="1"
        class="w-full"
      />
      <p class="muted mt-1 flex justify-between text-xs">
        <span>{{ t('reconstruction.compareResult') }}</span>
        <span>{{ t('reconstruction.compareBest') }}</span>
      </p>
    </section>

    <section v-if="finished && result" class="surface p-4">
      <h2 class="text-lg font-semibold">{{ t('reconstruction.uncertainty') }}</h2>
      <p class="muted mt-1 text-sm">{{ t('reconstruction.uncertaintyHint') }}</p>
      <img
        v-if="result.uncertainty_map_url"
        :src="result.uncertainty_map_url"
        :alt="t('reconstruction.uncertainty')"
        class="mt-3 w-full rounded-lg border border-night-700"
        loading="lazy"
      />
      <p v-else class="muted mt-3 text-sm">{{ t('reconstruction.uncertaintyMissing') }}</p>
    </section>

    <section v-if="metricEntries.length > 0" class="surface p-4">
      <h2 class="text-lg font-semibold">{{ t('reconstruction.metrics') }}</h2>
      <dl class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        <div v-for="[key, value] in metricEntries" :key="key" class="flex justify-between gap-3">
          <dt class="muted text-sm">{{ metricLabel(key) }}</dt>
          <dd class="font-mono text-sm">{{ round(value, 4) }}</dd>
        </div>
      </dl>
    </section>

    <section class="surface p-4">
      <h2 class="text-lg font-semibold">{{ t('reconstruction.result') }}</h2>
      <dl class="mt-3 grid gap-2 sm:grid-cols-2">
        <div class="flex justify-between gap-3">
          <dt class="muted text-sm">{{ t('reconstruction.pipelineVersion') }}</dt>
          <dd class="font-mono text-sm">{{ job.pipeline_version }}</dd>
        </div>
        <div class="flex justify-between gap-3">
          <dt class="muted text-sm">{{ t('reconstruction.inputCount') }}</dt>
          <dd class="font-mono text-sm">{{ job.input_count }}</dd>
        </div>
        <div class="flex justify-between gap-3">
          <dt class="muted text-sm">{{ t('reconstruction.computeTime') }}</dt>
          <dd class="font-mono text-sm">{{ formatExposure(job.compute_seconds) }}</dd>
        </div>
        <div class="flex justify-between gap-3">
          <dt class="muted text-sm">{{ t('reconstruction.cost') }}</dt>
          <dd class="font-mono text-sm">
            {{ job.cost_usd_estimate === null || job.cost_usd_estimate === undefined
              ? t('common.notAvailable')
              : `$${round(job.cost_usd_estimate, 2)}` }}
          </dd>
        </div>
        <div class="flex justify-between gap-3">
          <dt class="muted text-sm">{{ t('reconstruction.licenseResult') }}</dt>
          <dd><LicenseBadge :code="job.license" show-link /></dd>
        </div>
      </dl>

      <p v-if="result" class="mt-4 flex flex-wrap gap-3 text-sm">
        <a
          :href="result.result_url"
          class="btn-primary"
          target="_blank"
          rel="noopener noreferrer"
        >
          {{ t('reconstruction.downloadResult') }}
        </a>
        <a
          v-if="result.fits_url"
          :href="result.fits_url"
          class="btn-secondary"
          target="_blank"
          rel="noopener noreferrer"
        >
          {{ t('reconstruction.downloadFits') }}
        </a>
        <a
          v-if="result.report_url"
          :href="result.report_url"
          class="btn-secondary"
          target="_blank"
          rel="noopener noreferrer"
        >
          {{ t('reconstruction.downloadReport') }}
        </a>
      </p>
    </section>

    <ProvenanceTable
      :inputs="inputs?.items ?? []"
      :attribution-url="result?.attribution_markdown_url ?? null"
      :provenance-url="result?.provenance_json_url ?? null"
    />
  </div>

  <p v-else class="muted" role="status">{{ t('common.loading') }}</p>
</template>
