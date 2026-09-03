<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useSeoMeta } from '#app'
import { useAuthStore } from '~/stores/auth'
import { formatBytes } from '~/lib/astro'

const { t } = useI18n()
const auth = useAuthStore()

useSeoMeta({ title: () => t('me.quota') })
</script>

<template>
  <div class="grid gap-6">
    <h1 class="text-2xl font-semibold">{{ t('me.quota') }}</h1>
    <p v-if="!auth.isAuthenticated" class="muted">{{ t('me.signInRequired') }}</p>

    <section v-else-if="auth.quota" class="surface grid gap-4 p-4">
      <div>
        <p class="field-label">{{ t('me.storage') }}</p>
        <div
          class="h-3 w-full overflow-hidden rounded bg-night-800"
          role="progressbar"
          :aria-valuenow="Math.round(auth.quotaRatio * 100)"
          aria-valuemin="0"
          aria-valuemax="100"
          :aria-label="t('me.storage')"
        >
          <div
            class="h-full bg-sky-500"
            :style="{ width: `${Math.round(auth.quotaRatio * 100)}%` }"
          />
        </div>
        <p class="muted mt-2 text-sm">
          {{
            t('me.storageUsed', {
              used: formatBytes(auth.quota.used_bytes),
              total: formatBytes(auth.quota.quota_bytes),
            })
          }}
        </p>
      </div>

      <dl class="grid gap-2 sm:grid-cols-2">
        <div class="flex justify-between gap-3">
          <dt class="muted text-sm">{{ t('me.jobsQueued') }}</dt>
          <dd class="font-mono text-sm">
            {{ auth.quota.jobs_queued_now }} / {{ auth.quota.max_queued_jobs }}
          </dd>
        </div>
        <div class="flex justify-between gap-3">
          <dt class="muted text-sm">{{ t('me.jobsToday') }}</dt>
          <dd class="font-mono text-sm">
            {{ auth.quota.jobs_today }} / {{ auth.quota.max_jobs_per_day }}
          </dd>
        </div>
      </dl>
    </section>
  </div>
</template>
