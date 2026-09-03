<script setup lang="ts">
import { onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useHead } from '#app'
import { useAuthStore } from '~/stores/auth'
import { useBuilderStore } from '~/stores/builder'

const { t, locale } = useI18n()

useHead({
  titleTemplate: (title?: string) => (title ? `${title} · astro-photos` : 'astro-photos'),
  htmlAttrs: { lang: locale },
})

const auth = useAuthStore()
const builder = useBuilderStore()

onMounted(() => {
  void auth.restore()
  builder.restore()
})
</script>

<template>
  <div class="min-h-dvh">
    <a
      href="#main"
      class="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-sky-500 focus:px-3 focus:py-2 focus:text-night-950"
    >
      {{ t('common.skipToContent') }}
    </a>
    <AppHeader />
    <main id="main" tabindex="-1" class="mx-auto w-full max-w-7xl px-4 py-8">
      <NuxtPage />
    </main>
    <AppFooter />
  </div>
</template>
