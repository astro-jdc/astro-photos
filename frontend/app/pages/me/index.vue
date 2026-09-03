<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useSeoMeta } from '#app'
import { useAuthStore } from '~/stores/auth'
import { LICENSES } from '~/lib/licensing'
import type { LicenseCode } from '~/types/domain'

const { t } = useI18n()
const auth = useAuthStore()

const displayName = ref('')
const bio = ref('')
const website = ref('')
const defaultLicense = ref<LicenseCode>('CC-BY-NC-4.0')
const saving = ref(false)
const savedAt = ref<number | null>(null)
const token = ref('')

watch(
  () => auth.profile,
  (profile) => {
    if (!profile) return
    displayName.value = profile.display_name
    bio.value = profile.bio ?? ''
    website.value = profile.website_url ?? ''
    defaultLicense.value = profile.default_license
  },
  { immediate: true },
)

const dirty = computed(
  () =>
    auth.profile !== null &&
    (displayName.value !== auth.profile.display_name ||
      bio.value !== (auth.profile.bio ?? '') ||
      website.value !== (auth.profile.website_url ?? '') ||
      defaultLicense.value !== auth.profile.default_license),
)

async function save() {
  saving.value = true
  try {
    await auth.updateProfile({
      display_name: displayName.value,
      bio: bio.value || null,
      website_url: website.value || null,
      default_license: defaultLicense.value,
    })
    savedAt.value = Date.now()
  } finally {
    saving.value = false
  }
}

async function applyToken() {
  if (!token.value.trim()) return
  auth.setSession({ token: token.value.trim(), refreshToken: null, expiresAt: null })
  await auth.fetchProfile()
  token.value = ''
}

useSeoMeta({ title: () => t('me.title') })
</script>

<template>
  <div class="grid gap-6">
    <header class="flex flex-wrap items-center gap-3">
      <h1 class="text-2xl font-semibold">{{ t('me.title') }}</h1>
      <nav class="ml-auto flex flex-wrap gap-2 text-sm" :aria-label="t('me.title')">
        <NuxtLink to="/me/photos" class="btn-ghost py-1">{{ t('me.photos') }}</NuxtLink>
        <NuxtLink to="/me/reconstructions" class="btn-ghost py-1">
          {{ t('me.reconstructions') }}
        </NuxtLink>
        <NuxtLink to="/me/quota" class="btn-ghost py-1">{{ t('me.quota') }}</NuxtLink>
      </nav>
    </header>

    <section v-if="!auth.isAuthenticated" class="surface p-4">
      <p>{{ t('me.signInRequired') }}</p>
      <p class="muted mt-2 text-sm">{{ t('me.signInHelp') }}</p>
      <label class="field-label mt-4" for="token">{{ t('me.tokenLabel') }}</label>
      <input id="token" v-model="token" class="field-input" type="password" autocomplete="off" />
      <button type="button" class="btn-primary mt-3" @click="applyToken">
        {{ t('me.tokenApply') }}
      </button>
    </section>

    <form v-else class="surface grid gap-4 p-4" @submit.prevent="save">
      <h2 class="text-lg font-semibold">{{ t('me.profile') }}</h2>

      <div>
        <label class="field-label" for="display-name">{{ t('me.displayName') }}</label>
        <input id="display-name" v-model="displayName" class="field-input" type="text" required />
        <p class="field-help">{{ t('me.displayNameHelp') }}</p>
      </div>

      <div>
        <label class="field-label" for="bio">{{ t('me.bio') }}</label>
        <textarea id="bio" v-model="bio" class="field-input" rows="3" />
      </div>

      <div>
        <label class="field-label" for="website">{{ t('me.website') }}</label>
        <input id="website" v-model="website" class="field-input" type="url" />
      </div>

      <div>
        <label class="field-label" for="default-license">{{ t('me.defaultLicense') }}</label>
        <select id="default-license" v-model="defaultLicense" class="field-input">
          <option v-for="license in LICENSES" :key="license.code" :value="license.code">
            {{ t(license.nameKey) }}
          </option>
        </select>
      </div>

      <div class="flex items-center gap-3">
        <button type="submit" class="btn-primary" :disabled="!dirty || saving">
          {{ saving ? t('common.saving') : t('common.save') }}
        </button>
        <span v-if="savedAt" class="muted text-sm" role="status">{{ t('common.saved') }}</span>
        <button type="button" class="btn-ghost ml-auto" @click="auth.logout()">
          {{ t('common.signOut') }}
        </button>
      </div>
    </form>
  </div>
</template>
