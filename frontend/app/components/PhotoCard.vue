<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useBuilderStore } from '~/stores/builder'
import { forbidsStackDerivatives } from '~/lib/licensing'
import type { PhotoSummary } from '~/types/domain'

const props = withDefaults(
  defineProps<{ photo: PhotoSummary; selectable?: boolean }>(),
  { selectable: true },
)

const { t } = useI18n()
const builder = useBuilderStore()

const title = computed(() => props.photo.title || t('photo.untitled'))
const inBuilder = computed(() => builder.has(props.photo.id))
/**
 * `PhotoSummaryOut` solo trae el código de licencia, no el consentimiento
 * `allow_derivatives_in_stacks`. Con el código se descartan ND y ARR, que es
 * la mayoría de los casos; un opt-out explícito sobre una licencia permisiva
 * no se ve hasta `POST /reconstructions/preview`, que es obligatorio antes de
 * lanzar nada y devuelve la foto en `blocked[]`.
 */
const stackable = computed(() => !forbidsStackDerivatives(props.photo.license))
</script>

<template>
  <article class="surface flex flex-col overflow-hidden">
    <NuxtLink :to="`/photos/${photo.id}`" class="block aspect-video overflow-hidden bg-black">
      <NuxtImg
        v-if="photo.thumb_url"
        :src="photo.thumb_url"
        :alt="title"
        class="h-full w-full object-cover"
        loading="lazy"
        format="webp"
      />
      <span v-else class="muted flex h-full items-center justify-center text-xs">
        {{ t('photo.notReady') }}
      </span>
    </NuxtLink>

    <div class="flex flex-1 flex-col gap-2 p-3">
      <NuxtLink :to="`/photos/${photo.id}`" class="font-medium leading-tight hover:underline">
        {{ title }}
      </NuxtLink>
      <p v-if="photo.captured_at_utc" class="muted text-xs">
        {{ photo.captured_at_utc.slice(0, 10) }}
      </p>

      <div class="mt-auto flex flex-wrap items-center gap-1">
        <QualityBadge :score="photo.quality_score" />
        <LicenseBadge :code="photo.license" />
      </div>

      <button
        v-if="selectable"
        type="button"
        class="mt-2"
        :class="inBuilder ? 'btn-secondary' : 'btn-primary'"
        :disabled="!stackable"
        :title="stackable ? undefined : t('photo.cannotStack')"
        @click="builder.toggle(photo)"
      >
        {{ inBuilder ? t('photo.removeFromBuilder') : t('photo.addToBuilder') }}
      </button>
      <p v-if="selectable && !stackable" class="muted text-xs">{{ t('photo.cannotStack') }}</p>
    </div>
  </article>
</template>
