<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { qualityTier, round } from '~/lib/astro'

const props = defineProps<{ score?: number | null }>()

const { t } = useI18n()
const tier = computed(() => qualityTier(props.score))

const classes: Record<string, string> = {
  unrated: 'border-night-600 text-night-300',
  low: 'border-rose-500/60 text-rose-300',
  fair: 'border-amber-500/60 text-amber-300',
  good: 'border-sky-500/60 text-sky-300',
  excellent: 'border-emerald-500/60 text-emerald-300',
}
</script>

<template>
  <span
    class="chip"
    :class="classes[tier]"
    :title="
      score === null || score === undefined
        ? t('quality.unratedHelp')
        : t('quality.score', { value: round(score, 2) })
    "
  >
    {{ t('quality.label') }}: {{ t(`quality.${tier}`) }}
  </span>
</template>
