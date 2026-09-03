<script setup lang="ts">
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
</script>

<template>
  <footer class="border-t border-night-800 px-4 py-8 text-sm">
    <div class="mx-auto flex w-full max-w-7xl flex-col gap-3">
      <p class="font-medium">{{ t('common.tagline') }}</p>
      <p class="muted max-w-3xl">{{ t('home.limits.aperture') }}</p>
    </div>
  </footer>
</template>
