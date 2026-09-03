<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useSeoMeta } from '#app'
import { useApi } from '~/composables/useApi'
import { useAuthStore } from '~/stores/auth'
import { ApiError } from '~/lib/apiClient'
import type { PagePhotoSummary, PhotoSummary } from '~/types/domain'

const { t } = useI18n()
const auth = useAuthStore()
const api = useApi()

const items = ref<PhotoSummary[]>([])
const cursor = ref<string | null>(null)
const pending = ref(false)
const exhausted = ref(false)
const error = ref<ApiError | null>(null)

async function load(reset = false) {
  const ownerId = auth.profile?.id
  if (!ownerId || pending.value) return
  pending.value = true
  error.value = null
  try {
    // `owner` no está documentado en docs/api.md; ver el resumen de discrepancias.
    const page = await api.get<PagePhotoSummary>('/photos', {
      owner: ownerId,
      limit: 48,
      cursor: reset ? null : cursor.value,
      sort: 'recent',
    })
    items.value = reset ? page.items : [...items.value, ...page.items]
    cursor.value = page.next_cursor ?? null
    exhausted.value = (page.next_cursor ?? null) === null
  } catch (e) {
    error.value = e instanceof ApiError ? e : new ApiError({ status: 0, title: 'unknown_error' })
  } finally {
    pending.value = false
  }
}

onMounted(() => void load(true))
watch(() => auth.profile?.id, () => void load(true))

useSeoMeta({ title: () => t('me.photos') })
</script>

<template>
  <div class="grid gap-6">
    <h1 class="text-2xl font-semibold">{{ t('me.photos') }}</h1>
    <p v-if="!auth.isAuthenticated" class="muted">{{ t('me.signInRequired') }}</p>

    <template v-else>
      <p
        v-if="error"
        class="rounded-lg border border-rose-500/50 bg-rose-500/10 px-3 py-2 text-sm"
        role="alert"
      >
        {{ error.title }}
      </p>

      <div v-if="items.length > 0" class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <PhotoCard v-for="photo in items" :key="photo.id" :photo="photo" :selectable="false" />
      </div>
      <p v-else-if="!pending" class="muted text-sm">{{ t('me.noPhotos') }}</p>

      <p v-if="pending" class="muted text-sm" role="status">{{ t('common.loading') }}</p>
      <button
        v-else-if="!exhausted && items.length > 0"
        type="button"
        class="btn-secondary w-fit"
        @click="load()"
      >
        {{ t('explore.loadMore') }}
      </button>
    </template>
  </div>
</template>
