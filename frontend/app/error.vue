<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { clearError } from '#app'
import type { NuxtError } from '#app'

const props = defineProps<{ error: NuxtError }>()

const { t } = useI18n()

function goHome() {
  return clearError({ redirect: '/' })
}
</script>

<template>
  <div class="mx-auto grid min-h-dvh max-w-3xl place-content-center gap-4 px-4 text-center">
    <h1 class="text-4xl font-semibold">{{ props.error.statusCode }}</h1>
    <p class="text-lg">
      {{ props.error.statusCode === 404 ? t('errors.pageNotFound') : t('errors.generic') }}
    </p>
    <button type="button" class="btn-primary mx-auto" @click="goHome">
      {{ t('errors.backHome') }}
    </button>
  </div>
</template>
