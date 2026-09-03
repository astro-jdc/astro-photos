<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useColorMode } from '#imports'
import { useAuthStore } from '~/stores/auth'
import { useBuilderStore } from '~/stores/builder'

const { t, locale, locales, setLocale } = useI18n()
const colorMode = useColorMode()
const auth = useAuthStore()
const builder = useBuilderStore()

const links = computed(() => [
  { to: '/', label: t('nav.home') },
  { to: '/explore', label: t('nav.explore') },
  { to: '/reconstructions', label: t('nav.reconstructions') },
  { to: '/upload', label: t('nav.upload') },
])

const availableLocales = computed(() =>
  locales.value.map((l) => (typeof l === 'string' ? { code: l, name: l } : l)),
)

function cycleTheme() {
  colorMode.preference = colorMode.value === 'dark' ? 'light' : 'dark'
}
</script>

<template>
  <header class="border-b border-night-800 bg-night-950/80 backdrop-blur">
    <nav
      class="mx-auto flex w-full max-w-7xl flex-wrap items-center gap-4 px-4 py-3"
      :aria-label="t('nav.home')"
    >
      <NuxtLink :to="'/'" class="text-lg font-semibold tracking-tight">
        {{ t('common.appName') }}
      </NuxtLink>

      <ul class="flex flex-1 flex-wrap items-center gap-1 text-sm">
        <li v-for="link in links" :key="link.to">
          <NuxtLink :to="link.to" class="btn-ghost">{{ link.label }}</NuxtLink>
        </li>
        <li>
          <NuxtLink to="/build" class="btn-ghost">
            {{ t('nav.build') }}
            <span
              v-if="builder.count > 0"
              class="ml-1 rounded-full bg-sky-500 px-1.5 text-xs font-semibold text-night-950"
            >
              {{ builder.count }}
            </span>
          </NuxtLink>
        </li>
      </ul>

      <div class="flex items-center gap-2">
        <label class="sr-only" for="locale-select">{{ t('common.language') }}</label>
        <select
          id="locale-select"
          class="field-input w-auto py-1 text-xs"
          :value="locale"
          @change="setLocale(($event.target as HTMLSelectElement).value as typeof locale)"
        >
          <option v-for="l in availableLocales" :key="l.code" :value="l.code">
            {{ l.name }}
          </option>
        </select>

        <button
          type="button"
          class="btn-secondary py-1 text-xs"
          :aria-label="t('common.theme')"
          @click="cycleTheme"
        >
          {{ colorMode.value === 'dark' ? t('common.themeLight') : t('common.themeDark') }}
        </button>

        <NuxtLink v-if="auth.isAuthenticated" to="/me" class="btn-secondary py-1 text-xs">
          {{ auth.profile?.display_name ?? t('nav.me') }}
        </NuxtLink>
        <NuxtLink v-else to="/me" class="btn-secondary py-1 text-xs">
          {{ t('common.signIn') }}
        </NuxtLink>
      </div>
    </nav>
  </header>
</template>
