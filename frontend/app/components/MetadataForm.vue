<script setup lang="ts">
/**
 * Todo lo que el EXIF no trae y sin lo cual una toma no se combina bien:
 * objeto (autocompletado contra `/objects`), telescopio, montura, filtro, si
 * ya viene apilada, y la precisión de ubicación con el mapa de lo que se
 * publica realmente.
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useObjectSearch } from '~/composables/useObjects'
import type {
  GeoPoint,
  LocationPrecision,
  PhotoCompleteRequest,
  SkyObject,
} from '~/types/domain'

const props = defineProps<{ modelValue: PhotoCompleteRequest; idPrefix?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: PhotoCompleteRequest] }>()

const { t } = useI18n()
const { results, pending: searching, search } = useObjectSearch()

const prefix = computed(() => props.idPrefix ?? 'meta')

const FILTERS = [
  'none',
  'UV/IR-cut',
  'L-eNhance',
  'Ha',
  'OIII',
  'SII',
  'L',
  'R',
  'G',
  'B',
] as const

const PRECISIONS: LocationPrecision[] = ['exact', 'city', 'country', 'hidden']

const objectTerm = ref('')
const selectedObject = ref<SkyObject | null>(null)
let debounce: ReturnType<typeof setTimeout> | null = null

watch(objectTerm, (term) => {
  if (debounce) clearTimeout(debounce)
  debounce = setTimeout(() => void search(term), 250)
})

function patch(changes: Partial<PhotoCompleteRequest>) {
  emit('update:modelValue', { ...props.modelValue, ...changes })
}

function patchEquipment(changes: Partial<NonNullable<PhotoCompleteRequest['equipment']>>) {
  patch({ equipment: { ...(props.modelValue.equipment ?? {}), ...changes } })
}

function pickObject(object: SkyObject) {
  selectedObject.value = object
  objectTerm.value = objectLabel(object)
  patch({ object_id: object.id })
}

function clearObject() {
  selectedObject.value = null
  objectTerm.value = ''
  patch({ object_id: null })
}

function objectLabel(object: SkyObject): string {
  const catalogued = `${object.catalog}${object.catalog_number}`
  const common = object.common_name_es ?? object.common_name
  return common ? `${catalogued} — ${common}` : catalogued
}

function onLocationPick(point: GeoPoint) {
  patch({ location: { ...(props.modelValue.location ?? {}), ...point } })
}

const equipment = computed(() => props.modelValue.equipment ?? {})
</script>

<template>
  <section class="surface p-4">
    <h2 class="text-lg font-semibold">{{ t('metadata.title') }}</h2>
    <p class="muted mt-1 text-sm">{{ t('metadata.subtitle') }}</p>

    <div class="mt-4 grid gap-4 md:grid-cols-2">
      <div class="md:col-span-2">
        <label class="field-label" :for="`${prefix}-title`">{{ t('metadata.photoTitle') }}</label>
        <input
          :id="`${prefix}-title`"
          class="field-input"
          type="text"
          :value="modelValue.title ?? ''"
          @input="patch({ title: ($event.target as HTMLInputElement).value || null })"
        />
      </div>

      <div class="md:col-span-2">
        <label class="field-label" :for="`${prefix}-description`">
          {{ t('metadata.description') }}
        </label>
        <textarea
          :id="`${prefix}-description`"
          class="field-input"
          rows="3"
          :value="modelValue.description ?? ''"
          @input="patch({ description: ($event.target as HTMLTextAreaElement).value || null })"
        />
      </div>

      <div class="md:col-span-2">
        <label class="field-label" :for="`${prefix}-object`">{{ t('metadata.object') }}</label>
        <input
          :id="`${prefix}-object`"
          v-model="objectTerm"
          class="field-input"
          type="text"
          role="combobox"
          aria-expanded="true"
          :aria-controls="`${prefix}-object-list`"
          autocomplete="off"
          :placeholder="t('metadata.objectPlaceholder')"
        />
        <p class="field-help">{{ t('metadata.objectHint') }}</p>

        <p v-if="selectedObject" class="mt-2 flex items-center gap-2 text-sm">
          <span>{{ t('metadata.objectSelected', { name: objectLabel(selectedObject) }) }}</span>
          <button type="button" class="btn-ghost py-0.5 text-xs" @click="clearObject">
            {{ t('metadata.clearObject') }}
          </button>
        </p>

        <ul
          v-else-if="objectTerm.length > 0"
          :id="`${prefix}-object-list`"
          class="mt-2 max-h-56 overflow-auto rounded-lg border border-night-700"
          role="listbox"
        >
          <li v-if="searching" class="muted px-3 py-2 text-sm">{{ t('common.loading') }}</li>
          <li v-else-if="results.length === 0" class="muted px-3 py-2 text-sm">
            {{ t('metadata.noObjectMatch') }}
          </li>
          <li v-for="object in results" v-else :key="object.id" role="option" :aria-selected="false">
            <button
              type="button"
              class="w-full px-3 py-2 text-left text-sm hover:bg-night-800"
              @click="pickObject(object)"
            >
              {{ objectLabel(object) }}
              <span class="muted ml-1 text-xs">{{ t(`object.types.${object.object_type}`) }}</span>
            </button>
          </li>
        </ul>
      </div>

      <div>
        <label class="field-label" :for="`${prefix}-captured`">
          {{ t('metadata.capturedAtLocal') }}
        </label>
        <input
          :id="`${prefix}-captured`"
          class="field-input"
          type="datetime-local"
          :value="modelValue.captured_at_local ?? ''"
          @input="patch({ captured_at_local: ($event.target as HTMLInputElement).value || null })"
        />
      </div>

      <div>
        <label class="field-label" :for="`${prefix}-offset`">{{ t('metadata.utcOffset') }}</label>
        <input
          :id="`${prefix}-offset`"
          class="field-input"
          type="number"
          step="15"
          :value="modelValue.utc_offset_minutes ?? ''"
          @input="
            patch({
              utc_offset_minutes:
                ($event.target as HTMLInputElement).value === ''
                  ? null
                  : Number(($event.target as HTMLInputElement).value),
            })
          "
        />
      </div>

      <div>
        <label class="field-label" :for="`${prefix}-telescope`">{{ t('metadata.telescope') }}</label>
        <input
          :id="`${prefix}-telescope`"
          class="field-input"
          type="text"
          :value="equipment.telescope_model ?? ''"
          @input="patchEquipment({ telescope_model: ($event.target as HTMLInputElement).value || null })"
        />
      </div>

      <div>
        <label class="field-label" :for="`${prefix}-mount`">{{ t('metadata.mount') }}</label>
        <input
          :id="`${prefix}-mount`"
          class="field-input"
          type="text"
          :value="equipment.mount_model ?? ''"
          @input="patchEquipment({ mount_model: ($event.target as HTMLInputElement).value || null })"
        />
      </div>

      <div>
        <label class="field-label" :for="`${prefix}-filter`">{{ t('metadata.filter') }}</label>
        <select
          :id="`${prefix}-filter`"
          class="field-input"
          :value="equipment.filter_name ?? 'none'"
          @change="patchEquipment({ filter_name: ($event.target as HTMLSelectElement).value })"
        >
          <option v-for="name in FILTERS" :key="name" :value="name">
            {{ name === 'none' ? t('metadata.filterNone') : name }}
          </option>
        </select>
      </div>

      <div>
        <label class="field-label" :for="`${prefix}-subframes`">{{ t('metadata.subFrames') }}</label>
        <input
          :id="`${prefix}-subframes`"
          class="field-input"
          type="number"
          min="1"
          :disabled="!equipment.is_stacked"
          :value="equipment.sub_frames ?? ''"
          @input="
            patchEquipment({
              sub_frames:
                ($event.target as HTMLInputElement).value === ''
                  ? null
                  : Number(($event.target as HTMLInputElement).value),
            })
          "
        />
      </div>

      <div class="flex items-center gap-2">
        <input
          :id="`${prefix}-stacked`"
          type="checkbox"
          :checked="equipment.is_stacked === true"
          @change="patchEquipment({ is_stacked: ($event.target as HTMLInputElement).checked })"
        />
        <label :for="`${prefix}-stacked`">{{ t('metadata.isStacked') }}</label>
      </div>

      <div class="flex items-center gap-2">
        <input
          :id="`${prefix}-tracked`"
          type="checkbox"
          :checked="equipment.is_tracked === true"
          @change="patchEquipment({ is_tracked: ($event.target as HTMLInputElement).checked })"
        />
        <label :for="`${prefix}-tracked`">{{ t('metadata.tracked') }}</label>
      </div>

      <div class="md:col-span-2">
        <label class="field-label" :for="`${prefix}-attribution`">
          {{ t('metadata.attributionName') }}
        </label>
        <input
          :id="`${prefix}-attribution`"
          class="field-input"
          type="text"
          :value="modelValue.attribution_name ?? ''"
          @input="patch({ attribution_name: ($event.target as HTMLInputElement).value || null })"
        />
        <p class="field-help">{{ t('metadata.attributionHint') }}</p>
      </div>

      <div class="md:col-span-2">
        <label class="field-label" :for="`${prefix}-precision`">
          {{ t('metadata.locationPrecision') }}
        </label>
        <select
          :id="`${prefix}-precision`"
          class="field-input"
          :value="modelValue.location_precision"
          @change="
            patch({
              location_precision: ($event.target as HTMLSelectElement).value as LocationPrecision,
            })
          "
        >
          <option v-for="value in PRECISIONS" :key="value" :value="value">
            {{ t(`photo.precision.${value}`) }}
          </option>
        </select>
        <p class="field-help">{{ t('metadata.locationPrecisionHelp') }}</p>
      </div>

      <div class="md:col-span-2">
        <LocationPrecisionMap
          :location="modelValue.location ?? null"
          :precision="modelValue.location_precision"
          @pick="onLocationPick"
        />
      </div>
    </div>
  </section>
</template>
