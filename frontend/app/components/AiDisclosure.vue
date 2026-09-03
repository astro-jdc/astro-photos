<script setup lang="ts">
/**
 * Etiqueta obligatoria para cualquier salida que haya pasado por un modelo
 * aprendido (CLAUDE.md, regla dura 2). No es decorativa: da acceso al mapa de
 * incertidumbre y a la comparación contra el apilado clásico.
 */
import { useI18n } from 'vue-i18n'

withDefaults(
  defineProps<{
    usesLearnedModel: boolean
    uncertaintyUrl?: string | null
    comparisonHref?: string | null
    compact?: boolean
  }>(),
  { uncertaintyUrl: null, comparisonHref: null, compact: false },
)

const { t } = useI18n()
</script>

<template>
  <span v-if="usesLearnedModel && compact" class="chip border-fuchsia-500/60 text-fuchsia-300">
    {{ t('ai.label') }}
  </span>

  <aside
    v-else-if="usesLearnedModel"
    class="rounded-xl border border-fuchsia-500/50 bg-fuchsia-500/10 p-4"
    role="note"
    data-testid="ai-disclosure"
  >
    <p class="flex items-center gap-2 font-semibold">
      <span class="chip border-fuchsia-400 text-fuchsia-200">{{ t('ai.label') }}</span>
      {{ t('ai.title') }}
    </p>
    <p class="mt-2 text-sm">{{ t('ai.body') }}</p>
    <p class="mt-3 flex flex-wrap gap-3 text-sm">
      <a
        v-if="uncertaintyUrl"
        :href="uncertaintyUrl"
        target="_blank"
        rel="noopener noreferrer"
        class="text-sky-300 underline"
      >
        {{ t('ai.seeUncertainty') }}
      </a>
      <a v-if="comparisonHref" :href="comparisonHref" class="text-sky-300 underline">
        {{ t('ai.seeComparison') }}
      </a>
    </p>
  </aside>

  <p v-else-if="!compact" class="muted text-sm">{{ t('ai.classicalOnly') }}</p>
</template>
