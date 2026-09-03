<script setup lang="ts">
/**
 * Constructor de reconstrucciones.
 *
 * Regla innegociable 4 del agente: **nunca se encola un job a ciegas**. El
 * botón de lanzar solo se habilita tras un `POST /reconstructions/preview`
 * exitoso sobre la selección actual, y el plan muestra fotos elegidas, fotos
 * bloqueadas con su motivo, licencia resultante y coste/tiempo estimados.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { useBuilderStore, PIPELINES, type PipelineId } from '~/stores/builder'
import { useAuthStore } from '~/stores/auth'
import { formatExposure, round } from '~/lib/astro'

const { t } = useI18n()
const router = useRouter()
const builder = useBuilderStore()
const auth = useAuthStore()

const blockedById = computed(
  () => new Map((builder.preview?.blocked ?? []).map((b) => [b.photo_id, b])),
)

const quotaBlocked = computed(() => auth.isAuthenticated && !auth.canQueueJob)

const launchDisabled = computed(
  () => !builder.canLaunch || !auth.isAuthenticated || quotaBlocked.value,
)

async function launch() {
  const created = await builder.launch()
  if (created) {
    builder.clear()
    await router.push(`/reconstructions/${created.id}`)
  }
}

function pipelineName(id: PipelineId): string {
  return t(`builder.pipelines.${id}.name`)
}
</script>

<template>
  <div class="grid gap-6 lg:grid-cols-[2fr_1fr]">
    <section class="surface p-4">
      <div class="flex flex-wrap items-center gap-3">
        <h2 class="text-lg font-semibold">{{ t('builder.frames') }}</h2>
        <span class="chip">{{ t('builder.frameCount', { count: builder.count }) }}</span>
        <button
          v-if="builder.count > 0"
          type="button"
          class="btn-ghost ml-auto py-1 text-xs"
          @click="builder.clear()"
        >
          {{ t('builder.clear') }}
        </button>
      </div>

      <p v-if="builder.count === 0" class="muted mt-4 text-sm">
        {{ t('builder.empty') }}
        <NuxtLink to="/explore" class="text-sky-300 underline">
          {{ t('builder.addFromExplore') }}
        </NuxtLink>
      </p>

      <ul v-else class="mt-4 grid gap-2">
        <li
          v-for="frame in builder.frames"
          :key="frame.id"
          class="flex items-center gap-3 rounded-lg border p-2"
          :class="
            blockedById.has(frame.id) ? 'border-rose-500/60 bg-rose-500/5' : 'border-night-800'
          "
        >
          <NuxtImg
            v-if="frame.thumb_url"
            :src="frame.thumb_url"
            :alt="frame.title || t('photo.untitled')"
            class="h-12 w-20 shrink-0 rounded object-cover"
            loading="lazy"
          />
          <div class="min-w-0 flex-1">
            <NuxtLink :to="`/photos/${frame.id}`" class="block truncate text-sm hover:underline">
              {{ frame.title || t('photo.untitled') }}
            </NuxtLink>
            <p class="muted truncate text-xs">
              {{ frame.captured_at_utc ? frame.captured_at_utc.slice(0, 10) : t('common.unknown') }}
            </p>
            <p
              v-if="blockedById.get(frame.id)"
              class="text-xs text-rose-300"
              data-testid="blocked-reason"
            >
              {{ blockedById.get(frame.id)?.reason }}
            </p>
          </div>
          <LicenseBadge :code="frame.license" />
          <QualityBadge :score="frame.quality_score" />
          <button type="button" class="btn-ghost py-1 text-xs" @click="builder.remove(frame.id)">
            {{ t('common.remove') }}
          </button>
        </li>
      </ul>
    </section>

    <aside class="flex flex-col gap-4">
      <section class="surface p-4">
        <label class="field-label" for="builder-pipeline">{{ t('builder.pipeline') }}</label>
        <select id="builder-pipeline" v-model="builder.pipeline" class="field-input">
          <option v-for="pipeline in PIPELINES" :key="pipeline.id" :value="pipeline.id">
            {{ pipelineName(pipeline.id) }}
          </option>
        </select>
        <p class="field-help">{{ t(`builder.pipelines.${builder.pipeline}.description`) }}</p>
        <p class="field-help">{{ t('builder.pipelineHelp') }}</p>
        <AiDisclosure v-if="builder.usesLearnedModel" class="mt-3" :uses-learned-model="true" />
      </section>

      <section class="surface p-4">
        <h2 class="text-lg font-semibold">{{ t('builder.preview.title') }}</h2>

        <p v-if="builder.count < 2" class="muted mt-2 text-sm">{{ t('builder.minFrames') }}</p>

        <p
          v-else-if="builder.licenseHint.blocked.length > 0 && !builder.preview"
          class="muted mt-2 text-sm"
        >
          {{ t('builder.licenseHintBlocked') }}
        </p>
        <p
          v-else-if="builder.licenseHint.license && !builder.preview"
          class="muted mt-2 text-sm"
        >
          {{ t('builder.licenseHint', { license: builder.licenseHint.license }) }}
        </p>

        <button
          type="button"
          class="btn-primary mt-3 w-full"
          :disabled="!builder.canPreview"
          data-testid="run-preview"
          @click="builder.runPreview()"
        >
          {{ builder.previewPending ? t('builder.preview.pending') : t('builder.preview.run') }}
        </button>

        <p
          v-if="builder.previewError"
          class="mt-3 rounded-lg border border-rose-500/50 bg-rose-500/10 px-3 py-2 text-sm"
          role="alert"
        >
          {{ builder.previewError.title }}
          <template v-if="builder.previewError.detail">
            — {{ builder.previewError.detail }}
          </template>
        </p>

        <p v-if="builder.isStale" class="mt-3 text-sm text-amber-300" role="status">
          {{ t('builder.preview.stale') }}
        </p>

        <template v-if="builder.preview">
          <dl class="mt-4 grid gap-2 text-sm">
            <div class="flex justify-between gap-2">
              <dt class="muted">{{ t('builder.preview.selected') }}</dt>
              <dd data-testid="preview-selected">{{ builder.preview.selected.length }}</dd>
            </div>
            <div class="flex justify-between gap-2">
              <dt class="muted">{{ t('builder.preview.blocked') }}</dt>
              <dd data-testid="preview-blocked">{{ builder.preview.blocked.length }}</dd>
            </div>
            <div class="flex justify-between gap-2">
              <dt class="muted">{{ t('builder.preview.license') }}</dt>
              <dd><LicenseBadge :code="builder.preview.resulting_license" /></dd>
            </div>
            <div class="flex justify-between gap-2">
              <dt class="muted">{{ t('builder.preview.time') }}</dt>
              <dd>{{ formatExposure(builder.preview.estimated_compute_seconds) }}</dd>
            </div>
            <div class="flex justify-between gap-2">
              <dt class="muted">{{ t('builder.preview.cost') }}</dt>
              <dd>${{ round(builder.preview.estimated_cost_usd ?? 0, 2) }}</dd>
            </div>
            <div
              v-if="builder.preview.estimated_queue_seconds !== null"
              class="flex justify-between gap-2"
            >
              <dt class="muted">{{ t('builder.preview.queue') }}</dt>
              <dd>{{ formatExposure(builder.preview.estimated_queue_seconds) }}</dd>
            </div>
          </dl>

          <p class="field-help">{{ t('builder.preview.licenseHelp') }}</p>

          <p v-if="builder.preview.uses_learned_model" class="mt-2 text-sm">
            {{ t('builder.preview.usesModel') }}
          </p>

          <ul
            v-if="builder.preview.blocked.length > 0"
            class="mt-3 grid gap-1 text-xs text-rose-300"
          >
            <li v-for="blocked in builder.preview.blocked" :key="blocked.photo_id">
              {{ blocked.photo_id }} — {{ blocked.reason }}
            </li>
          </ul>
          <button
            v-if="builder.preview.blocked.length > 0"
            type="button"
            class="btn-secondary mt-3 w-full"
            @click="builder.dropBlocked()"
          >
            {{ t('builder.preview.dropBlocked') }}
          </button>
        </template>

        <p v-else class="muted mt-3 text-sm">{{ t('builder.preview.required') }}</p>
      </section>

      <section class="surface p-4">
        <button
          type="button"
          class="btn-primary w-full"
          :disabled="launchDisabled"
          data-testid="launch-reconstruction"
          @click="launch"
        >
          {{ builder.submitting ? t('builder.launching') : t('builder.launch') }}
        </button>

        <p v-if="!auth.isAuthenticated" class="muted mt-2 text-sm">
          {{ t('builder.signInRequired') }}
        </p>
        <p v-else-if="quotaBlocked" class="mt-2 text-sm text-amber-300" role="status">
          {{ t('builder.quotaBlocked') }}
        </p>
        <p v-else-if="!builder.preview || builder.isStale" class="muted mt-2 text-sm">
          {{ t('builder.preview.required') }}
        </p>

        <p
          v-if="builder.submitError"
          class="mt-3 rounded-lg border border-rose-500/50 bg-rose-500/10 px-3 py-2 text-sm"
          role="alert"
        >
          {{ builder.submitError.title }}
        </p>
      </section>
    </aside>
  </div>
</template>
