<script setup lang="ts">
/**
 * Selector de licencia.
 *
 * Reglas de docs/licensing.md que este componente implementa literalmente:
 *  - `CC-BY-NC-4.0` viene preseleccionada (o la `default_license` del perfil).
 *  - Cada opción se explica en lenguaje llano, no solo con su sigla.
 *  - `allow_ai_training` y `allow_derivatives_in_stacks` son dos casillas
 *    **independientes** de la licencia.
 *  - Un ND (o ARR) fuerza `allow_derivatives_in_stacks = false` y la UI lo
 *    explica en vez de dejar un estado incoherente.
 *  - Se avisa de que la licencia se congela tras la primera descarga.
 */
import { computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { DEFAULT_LICENSE, LICENSES, forbidsStackDerivatives } from '~/lib/licensing'
import type { LicenseCode } from '~/types/domain'

const props = withDefaults(
  defineProps<{
    modelValue?: LicenseCode
    allowAiTraining?: boolean
    allowDerivativesInStacks?: boolean
    /** ISO-8601 si `photos.license_locked_at` no es NULL. */
    lockedAt?: string | null
    disabled?: boolean
    idPrefix?: string
  }>(),
  {
    modelValue: DEFAULT_LICENSE,
    allowAiTraining: true,
    allowDerivativesInStacks: true,
    lockedAt: null,
    disabled: false,
    idPrefix: 'license',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: LicenseCode]
  'update:allowAiTraining': [value: boolean]
  'update:allowDerivativesInStacks': [value: boolean]
}>()

const { t, d } = useI18n()

const licenses = computed(() => LICENSES)
const selected = computed(() => props.modelValue)
const ndForced = computed(() => forbidsStackDerivatives(props.modelValue))
const isLocked = computed(() => props.lockedAt !== null && props.lockedAt !== undefined)

/** Con la licencia congelada solo se puede relajar (bajar restrictividad). */
function isDisabledOption(code: LicenseCode): boolean {
  if (props.disabled) return true
  if (!isLocked.value) return false
  const current = LICENSES.find((l) => l.code === props.modelValue)
  const candidate = LICENSES.find((l) => l.code === code)
  if (!current || !candidate) return false
  return candidate.restrictiveness > current.restrictiveness
}

function select(code: LicenseCode) {
  if (isDisabledOption(code)) return
  emit('update:modelValue', code)
}

// Coherencia forzosa: ND ⇒ no puede usarse como frame.
watch(
  ndForced,
  (forced) => {
    if (forced && props.allowDerivativesInStacks) emit('update:allowDerivativesInStacks', false)
  },
  { immediate: true },
)

const lockedLabel = computed(() => {
  if (!props.lockedAt) return ''
  const parsed = new Date(props.lockedAt)
  const date = Number.isNaN(parsed.getTime()) ? props.lockedAt : d(parsed, 'short')
  return t('license.picker.freezeLocked', { date })
})
</script>

<template>
  <fieldset class="surface p-4" data-testid="license-picker">
    <legend class="px-1 text-base font-semibold">{{ t('license.picker.title') }}</legend>
    <p class="muted mb-3 text-sm">{{ t('license.picker.subtitle') }}</p>

    <p
      v-if="isLocked"
      class="mb-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm"
      role="status"
    >
      {{ lockedLabel }}
    </p>
    <p v-else class="muted mb-3 text-sm">{{ t('license.picker.freezeWarning') }}</p>

    <ul class="grid gap-2">
      <li v-for="license in licenses" :key="license.code">
        <label
          class="flex cursor-pointer gap-3 rounded-lg border p-3 transition-colors"
          :class="[
            selected === license.code
              ? 'border-sky-400 bg-sky-500/10'
              : 'border-night-700 hover:border-night-500',
            isDisabledOption(license.code) ? 'cursor-not-allowed opacity-50' : '',
          ]"
        >
          <input
            :id="`${idPrefix}-${license.code}`"
            type="radio"
            class="mt-1"
            :name="`${idPrefix}-license`"
            :value="license.code"
            :checked="selected === license.code"
            :disabled="isDisabledOption(license.code)"
            :data-testid="`license-option-${license.code}`"
            @change="select(license.code)"
          />
          <span class="min-w-0">
            <span class="flex flex-wrap items-center gap-2">
              <span class="font-medium">{{ t(license.nameKey) }}</span>
              <span
                v-if="license.code === DEFAULT_LICENSE"
                class="chip border-sky-400 text-sky-300"
                data-testid="license-default-badge"
              >
                {{ t('license.picker.default') }}
              </span>
            </span>
            <span class="muted mt-1 block text-sm">{{ t(license.descriptionKey) }}</span>
            <span class="mt-2 flex flex-wrap gap-1 text-xs">
              <span class="chip">
                {{ t('license.picker.commercial') }}:
                {{ license.allowsCommercial ? t('common.yes') : t('common.no') }}
              </span>
              <span class="chip">
                {{ t('license.picker.derivatives') }}:
                {{ license.allowsDerivatives ? t('common.yes') : t('common.no') }}
              </span>
              <span v-if="license.requiresShareAlike" class="chip">
                {{ t('license.picker.shareAlike') }}
              </span>
              <span v-if="license.requiresAttribution" class="chip">
                {{ t('license.picker.attribution') }}
              </span>
            </span>
            <a
              v-if="license.url"
              :href="license.url"
              target="_blank"
              rel="noopener noreferrer"
              class="mt-2 inline-block text-xs text-sky-300 underline"
            >
              {{ t('license.picker.readMore') }}
            </a>
          </span>
        </label>
      </li>
    </ul>

    <div class="mt-4 grid gap-3 border-t border-night-800 pt-4">
      <div>
        <label class="flex items-start gap-2">
          <input
            :checked="allowAiTraining"
            type="checkbox"
            class="mt-1"
            :disabled="disabled"
            data-testid="allow-ai-training"
            @change="
              emit('update:allowAiTraining', ($event.target as HTMLInputElement).checked)
            "
          />
          <span>
            <span class="font-medium">{{ t('license.picker.allowAiTraining') }}</span>
            <span class="field-help block">{{ t('license.picker.allowAiTrainingHelp') }}</span>
          </span>
        </label>
      </div>

      <div>
        <label class="flex items-start gap-2">
          <input
            :checked="allowDerivativesInStacks && !ndForced"
            type="checkbox"
            class="mt-1"
            :disabled="disabled || ndForced"
            data-testid="allow-derivatives-in-stacks"
            @change="
              emit('update:allowDerivativesInStacks', ($event.target as HTMLInputElement).checked)
            "
          />
          <span>
            <span class="font-medium">{{ t('license.picker.allowDerivatives') }}</span>
            <span class="field-help block">{{ t('license.picker.allowDerivativesHelp') }}</span>
          </span>
        </label>
        <p
          v-if="ndForced"
          class="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm"
          role="status"
          data-testid="nd-forced-notice"
        >
          {{ t('license.picker.ndForcesNoStacks') }}
        </p>
      </div>
    </div>
  </fieldset>
</template>
