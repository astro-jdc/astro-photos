<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useSeoMeta } from '#app'
import { useReconstructions } from '~/composables/useReconstruction'
import { useAuthStore } from '~/stores/auth'
import type { Reconstruction } from '~/types/domain'

const { t } = useI18n()
const auth = useAuthStore()
const { list } = useReconstructions()

const items = ref<Reconstruction[]>([])
const pending = ref(false)

async function load() {
  if (!auth.isAuthenticated) return
  pending.value = true
  try {
    const page = await list({ mine: true, limit: 50 })
    items.value = page.items
  } finally {
    pending.value = false
  }
}

onMounted(() => void load())

useSeoMeta({ title: () => t('me.reconstructions') })
</script>

<template>
  <div class="grid gap-6">
    <h1 class="text-2xl font-semibold">{{ t('me.reconstructions') }}</h1>
    <p v-if="!auth.isAuthenticated" class="muted">{{ t('me.signInRequired') }}</p>
    <template v-else>
      <ul v-if="items.length > 0" class="grid gap-3">
        <li v-for="job in items" :key="job.id" class="surface flex flex-wrap items-center gap-3 p-3">
          <NuxtLink :to="`/reconstructions/${job.id}`" class="font-medium hover:underline">
            {{ job.object_name ?? job.pipeline }}
          </NuxtLink>
          <span class="chip">{{ t(`reconstruction.status.${job.status}`) }}</span>
          <LicenseBadge :code="job.license" />
          <AiDisclosure compact :uses-learned-model="job.uses_learned_model" />
          <span class="muted ml-auto text-xs">{{ job.created_at }}</span>
        </li>
      </ul>
      <p v-else-if="!pending" class="muted text-sm">{{ t('me.noReconstructions') }}</p>
      <p v-if="pending" class="muted text-sm" role="status">{{ t('common.loading') }}</p>
    </template>
  </div>
</template>
