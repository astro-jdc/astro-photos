<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { licenseFacts } from '~/lib/licensing'
import type { LicenseCode } from '~/types/domain'

const props = withDefaults(defineProps<{ code: LicenseCode; showLink?: boolean }>(), {
  showLink: false,
})

const { t } = useI18n()
const facts = computed(() => licenseFacts(props.code))
</script>

<template>
  <span class="inline-flex items-center gap-1">
    <span
      class="chip font-mono"
      :class="
        facts.allowsDerivatives ? 'border-emerald-500/60 text-emerald-300' : 'border-amber-500/60 text-amber-300'
      "
      :title="t(facts.descriptionKey)"
    >
      {{ code }}
    </span>
    <a
      v-if="showLink && facts.url"
      :href="facts.url"
      target="_blank"
      rel="noopener noreferrer"
      class="text-xs text-sky-300 underline"
    >
      {{ t('license.picker.readMore') }}
    </a>
  </span>
</template>
