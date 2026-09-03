<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useSeoMeta } from '#app'
import { useAuthStore } from '~/stores/auth'
import { useUploadStore, type UploadItem } from '~/stores/upload'
import { DEFAULT_LICENSE } from '~/lib/licensing'
import type { LicenseCode, PhotoCompleteRequest } from '~/types/domain'

const { t } = useI18n()
const auth = useAuthStore()
const store = useUploadStore()

const expanded = ref<string | null>(null)

const defaultLicense = computed<LicenseCode>(
  () => auth.profile?.default_license ?? DEFAULT_LICENSE,
)

function onEnqueued(ids: string[]) {
  expanded.value = ids[0] ?? null
}

function metadataOf(item: UploadItem): PhotoCompleteRequest {
  return item.metadata
}

function updateMetadata(item: UploadItem, value: PhotoCompleteRequest) {
  store.setMetadata(item.id, value)
}

useSeoMeta({ title: () => t('upload.title'), description: () => t('upload.subtitle') })
</script>

<template>
  <div class="grid gap-6">
    <header class="grid gap-2">
      <h1 class="text-2xl font-semibold">{{ t('upload.title') }}</h1>
      <p class="muted">{{ t('upload.subtitle') }}</p>
    </header>

    <p
      v-if="!auth.isAuthenticated"
      class="surface border-amber-500/40 bg-amber-500/10 p-4 text-sm"
      role="status"
    >
      {{ t('upload.signInRequired') }}
      <NuxtLink to="/me" class="ml-2 text-sky-300 underline">{{ t('common.signIn') }}</NuxtLink>
    </p>

    <UploadDropzone
      :default-license="defaultLicense"
      :disabled="!auth.isAuthenticated"
      @enqueued="onEnqueued"
    >
      <template #item="{ item }">
        <div class="mt-3">
          <button
            type="button"
            class="btn-ghost py-1 text-xs"
            :aria-expanded="expanded === item.id"
            @click="expanded = expanded === item.id ? null : item.id"
          >
            {{ expanded === item.id ? t('common.showLess') : t('metadata.title') }}
          </button>

          <div v-if="expanded === item.id" class="mt-3 grid gap-4">
            <MetadataForm
              :id-prefix="`meta-${item.id}`"
              :model-value="metadataOf(item)"
              @update:model-value="updateMetadata(item, $event)"
            />
            <LicensePicker
              :id-prefix="`lic-${item.id}`"
              :model-value="item.metadata.license"
              :allow-ai-training="item.metadata.allow_ai_training"
              :allow-derivatives-in-stacks="item.metadata.allow_derivatives_in_stacks"
              @update:model-value="store.setMetadata(item.id, { license: $event })"
              @update:allow-ai-training="store.setMetadata(item.id, { allow_ai_training: $event })"
              @update:allow-derivatives-in-stacks="
                store.setMetadata(item.id, { allow_derivatives_in_stacks: $event })
              "
            />
          </div>
        </div>
      </template>
    </UploadDropzone>

    <div v-if="store.items.length > 0" class="flex flex-wrap items-center gap-3">
      <button
        type="button"
        class="btn-primary"
        :disabled="!auth.isAuthenticated || store.isBusy"
        data-testid="start-all-uploads"
        @click="store.startAll()"
      >
        {{ t('upload.queue.startAll') }}
      </button>
      <button type="button" class="btn-secondary" @click="store.clearFinished()">
        {{ t('upload.queue.clearFinished') }}
      </button>
      <p class="muted text-sm" aria-live="polite">
        {{ t('upload.queue.progress', { done: store.succeeded.length, total: store.items.length }) }}
      </p>
    </div>

    <p
      v-if="store.succeeded.length > 0"
      class="surface border-emerald-500/40 bg-emerald-500/10 p-4 text-sm"
      role="status"
      data-testid="upload-success"
    >
      {{
        store.succeeded.length === 1
          ? t('upload.successOne')
          : t('upload.successMany', { count: store.succeeded.length })
      }}
    </p>
  </div>
</template>
