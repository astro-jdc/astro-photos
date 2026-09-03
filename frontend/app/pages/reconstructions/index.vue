<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useSeoMeta } from '#app'
import { useReconstructions } from '~/composables/useReconstruction'
import { ApiError } from '~/lib/apiClient'
import type { Reconstruction } from '~/types/domain'

const { t } = useI18n()
const { list } = useReconstructions()

const items = ref<Reconstruction[]>([])
const cursor = ref<string | null>(null)
const pending = ref(false)
const error = ref<ApiError | null>(null)
const exhausted = ref(false)

async function load(reset = false) {
  if (pending.value) return
  pending.value = true
  error.value = null
  try {
    const page = await list({ cursor: reset ? null : cursor.value })
    items.value = reset ? page.items : [...items.value, ...page.items]
    cursor.value = page.next_cursor
    exhausted.value = page.next_cursor === null
  } catch (e) {
    error.value = e instanceof ApiError ? e : new ApiError({ status: 0, title: 'unknown_error' })
  } finally {
    pending.value = false
  }
}

onMounted(() => void load(true))

useSeoMeta({
  title: () => t('reconstruction.listTitle'),
  description: () => t('reconstruction.listSubtitle'),
})
</script>

<template>
  <div class="grid gap-6">
    <header class="grid gap-2">
      <h1 class="text-2xl font-semibold">{{ t('reconstruction.listTitle') }}</h1>
      <p class="muted">{{ t('reconstruction.listSubtitle') }}</p>
    </header>

    <p
      v-if="error"
      class="rounded-lg border border-rose-500/50 bg-rose-500/10 px-3 py-2 text-sm"
      role="alert"
    >
      {{ error.title }}
    </p>

    <ul v-if="items.length > 0" class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      <li v-for="job in items" :key="job.id" class="surface overflow-hidden">
        <NuxtLink :to="`/reconstructions/${job.id}`" class="block aspect-video bg-black">
          <NuxtImg
            v-if="job.preview_url"
            :src="job.preview_url"
            :alt="job.object_name ?? job.pipeline"
            class="h-full w-full object-cover"
            loading="lazy"
          />
        </NuxtLink>
        <div class="grid gap-2 p-3">
          <NuxtLink :to="`/reconstructions/${job.id}`" class="font-medium hover:underline">
            {{ job.object_name ?? job.pipeline }}
          </NuxtLink>
          <p class="muted text-xs">
            {{ t(`reconstruction.status.${job.status}`) }} ·
            {{ t('reconstruction.inputCount') }}: {{ job.input_count }}
          </p>
          <div class="flex flex-wrap items-center gap-2">
            <LicenseBadge :code="job.license" />
            <AiDisclosure compact :uses-learned-model="job.uses_learned_model" />
          </div>
        </div>
      </li>
    </ul>
    <p v-else-if="!pending" class="muted text-sm">{{ t('reconstruction.listEmpty') }}</p>

    <p v-if="pending" class="muted text-sm" role="status">{{ t('common.loading') }}</p>
    <button
      v-else-if="!exhausted && items.length > 0"
      type="button"
      class="btn-secondary w-fit"
      @click="load()"
    >
      {{ t('explore.loadMore') }}
    </button>
  </div>
</template>
