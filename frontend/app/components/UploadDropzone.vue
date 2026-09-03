<script setup lang="ts">
/**
 * Arrastrar y soltar + lectura de EXIF en el navegador. El fichero se encola
 * en el store `upload`, que se encarga del POST presignado / multipart y de la
 * barra de progreso. El binario nunca pasa por el backend.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useUploadStore } from '~/stores/upload'
import { ACCEPTED_EXTENSIONS, MULTIPART_THRESHOLD_BYTES } from '~/lib/upload'
import { formatBytes } from '~/lib/astro'
import { DEFAULT_LICENSE } from '~/lib/licensing'
import type { LicenseCode } from '~/types/domain'

const props = withDefaults(defineProps<{ defaultLicense?: LicenseCode; disabled?: boolean }>(), {
  defaultLicense: DEFAULT_LICENSE,
  disabled: false,
})

const emit = defineEmits<{ enqueued: [ids: string[]] }>()

const { t } = useI18n()
const store = useUploadStore()

const input = ref<HTMLInputElement | null>(null)
const dragging = ref(false)
const reading = ref(false)

const accept = computed(() => ACCEPTED_EXTENSIONS.join(','))
const acceptedLabel = computed(() => ACCEPTED_EXTENSIONS.join(' '))

async function handle(files: FileList | null) {
  if (!files || files.length === 0 || props.disabled) return
  reading.value = true
  try {
    const ids = await store.enqueue([...files], props.defaultLicense)
    emit('enqueued', ids)
  } finally {
    reading.value = false
  }
}

function onDrop(event: DragEvent) {
  dragging.value = false
  void handle(event.dataTransfer?.files ?? null)
}

function onChange(event: Event) {
  const target = event.target as HTMLInputElement
  void handle(target.files)
  target.value = ''
}

function openPicker() {
  input.value?.click()
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    openPicker()
  }
}
</script>

<template>
  <div>
    <div
      class="surface flex flex-col items-center justify-center gap-2 border-2 border-dashed p-10 text-center transition-colors"
      :class="[
        dragging ? 'border-sky-400 bg-sky-500/10' : 'border-night-700',
        disabled ? 'opacity-50' : 'cursor-pointer',
      ]"
      role="button"
      tabindex="0"
      :aria-disabled="disabled"
      :aria-label="t('upload.dropzone.prompt')"
      data-testid="upload-dropzone"
      @click="openPicker"
      @keydown="onKeydown"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="onDrop"
    >
      <p class="text-lg font-medium">
        {{ dragging ? t('upload.dropzone.dropNow') : t('upload.dropzone.prompt') }}
      </p>
      <p class="muted text-sm">{{ t('upload.dropzone.hint') }}</p>
      <button type="button" class="btn-secondary mt-2" :disabled="disabled" @click.stop="openPicker">
        {{ t('upload.dropzone.browse') }}
      </button>
      <p class="muted mt-2 font-mono text-xs">
        {{ t('upload.dropzone.accepted', { list: acceptedLabel }) }}
      </p>
      <p v-if="reading" class="text-sm" role="status">{{ t('upload.dropzone.reading') }}</p>

      <input
        ref="input"
        type="file"
        multiple
        class="sr-only"
        :accept="accept"
        :disabled="disabled"
        data-testid="upload-input"
        @change="onChange"
      />
    </div>

    <ul v-if="store.items.length > 0" class="mt-6 grid gap-3">
      <li v-for="item in store.items" :key="item.id" class="surface p-3">
        <div class="flex flex-wrap items-center gap-2">
          <span class="truncate font-medium">{{ item.file.name }}</span>
          <span class="muted text-xs">{{ formatBytes(item.bytes) }}</span>
          <span class="chip">{{ t(`upload.state.${item.state}`) }}</span>
          <span v-if="item.bytes > MULTIPART_THRESHOLD_BYTES" class="chip">
            {{ t('upload.multipart') }}
          </span>
          <span class="ml-auto flex gap-2">
            <button
              v-if="item.state === 'error'"
              type="button"
              class="btn-secondary py-1 text-xs"
              @click="store.retry(item.id)"
            >
              {{ t('upload.retry') }}
            </button>
            <button
              type="button"
              class="btn-ghost py-1 text-xs"
              @click="store.remove(item.id)"
            >
              {{ t('common.remove') }}
            </button>
          </span>
        </div>

        <div
          class="mt-2 h-2 w-full overflow-hidden rounded bg-night-800"
          role="progressbar"
          :aria-valuenow="Math.round(item.progress * 100)"
          aria-valuemin="0"
          aria-valuemax="100"
          :aria-label="item.file.name"
        >
          <div
            class="h-full bg-sky-500 transition-[width]"
            :style="{ width: `${Math.round(item.progress * 100)}%` }"
          />
        </div>

        <p v-if="item.exif && !item.exif.empty" class="muted mt-2 text-xs">
          {{ t('upload.exif.found') }} {{ t('upload.exif.hint') }}
        </p>
        <p v-else-if="item.exif" class="muted mt-2 text-xs">{{ t('upload.exif.none') }}</p>

        <p v-if="item.state === 'error'" class="mt-2 text-sm text-rose-300" role="alert">
          {{ item.errorTitle }}<template v-if="item.errorDetail">: {{ item.errorDetail }}</template>
        </p>

        <slot name="item" :item="item" />
      </li>
    </ul>
  </div>
</template>
