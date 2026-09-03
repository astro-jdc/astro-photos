<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { licenseFacts } from '~/lib/licensing'
import type { LicenseCode } from '~/types/domain'

/**
 * `code` admite `null`/`undefined` porque la licencia de una reconstrucción
 * solo existe cuando el job ha resuelto sus entradas: mientras está en cola
 * viaja como `null`. En ese caso no se pinta nada, en vez de inventar una
 * licencia por defecto — que sería justo el bug legal que hay que evitar.
 */
const props = withDefaults(
  defineProps<{ code?: LicenseCode | null; showLink?: boolean }>(),
  { code: null, showLink: false },
)

const { t } = useI18n()
const facts = computed(() => (props.code ? licenseFacts(props.code) : null))
</script>

<template>
  <span v-if="code && facts" class="inline-flex items-center gap-1">
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
