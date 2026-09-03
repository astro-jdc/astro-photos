<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useSeoMeta } from '#app'

const { t } = useI18n()

useSeoMeta({ title: () => t('builder.title'), description: () => t('builder.subtitle') })
</script>

<template>
  <div class="grid gap-6">
    <header class="grid gap-2">
      <h1 class="text-2xl font-semibold">{{ t('builder.title') }}</h1>
      <p class="muted">{{ t('builder.subtitle') }}</p>
    </header>
    <ReconstructionBuilder />
  </div>
</template>
