<script setup lang="ts">
/**
 * Enseña **exactamente** lo que se va a publicar según `location_precision`:
 * un punto exacto, un círculo a nivel de ciudad, el país entero, o nada.
 * Sin sorpresas después.
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRuntimeConfig } from '#app'
import { round } from '~/lib/astro'
import type { GeoPointIn, LocationPrecision } from '~/types/domain'

const props = defineProps<{
  location: GeoPointIn | null
  precision: LocationPrecision
}>()

const emit = defineEmits<{ pick: [point: GeoPointIn] }>()

const { t } = useI18n()
const config = useRuntimeConfig()

const container = ref<HTMLDivElement | null>(null)
const failed = ref(false)
const map = shallowRef<{ remove: () => void } | null>(null)

/** Radio de ofuscación, en metros, que corresponde a cada precisión. */
const OBFUSCATION_M: Record<LocationPrecision, number> = {
  exact: 0,
  city: 10_000,
  country: 300_000,
  hidden: 0,
}

const published = computed<GeoPointIn | null>(() => {
  if (!props.location || props.precision === 'hidden') return null
  if (props.precision === 'exact') return props.location
  const step = props.precision === 'city' ? 0.1 : 1
  return {
    ...props.location,
    lat: Math.round(props.location.lat / step) * step,
    lon: Math.round(props.location.lon / step) * step,
  }
})

const label = computed(() => {
  if (props.precision === 'hidden' || !published.value) return t('photo.precision.hidden')
  return `${round(published.value.lat, props.precision === 'exact' ? 5 : 2)}, ${round(
    published.value.lon,
    props.precision === 'exact' ? 5 : 2,
  )}`
})

async function initMap() {
  if (!container.value) return
  try {
    const maplibre = await import('maplibre-gl')
    const instance = new maplibre.Map({
      container: container.value,
      style: String(config.public.mapStyleUrl),
      center: [props.location?.lon ?? 0, props.location?.lat ?? 20],
      zoom: props.location ? 6 : 1,
      attributionControl: { compact: true },
    })
    instance.on('click', (event: { lngLat: { lat: number; lng: number } }) => {
      emit('pick', { lat: event.lngLat.lat, lon: event.lngLat.lng })
    })
    instance.on('load', () => refreshLayer(instance))
    map.value = instance
  } catch {
    failed.value = true
  }
}

interface MapLike {
  getSource: (id: string) => { setData: (data: unknown) => void } | undefined
  addSource: (id: string, source: unknown) => void
  addLayer: (layer: unknown) => void
  flyTo: (options: unknown) => void
}

function geojson() {
  return {
    type: 'FeatureCollection' as const,
    features: published.value
      ? [
          {
            type: 'Feature' as const,
            geometry: {
              type: 'Point' as const,
              coordinates: [published.value.lon, published.value.lat],
            },
            properties: { radius: OBFUSCATION_M[props.precision] },
          },
        ]
      : [],
  }
}

function refreshLayer(instance: unknown) {
  const m = instance as MapLike
  const existing = m.getSource('published-location')
  if (existing) {
    existing.setData(geojson())
    return
  }
  m.addSource('published-location', { type: 'geojson', data: geojson() })
  m.addLayer({
    id: 'published-location-circle',
    type: 'circle',
    source: 'published-location',
    paint: {
      'circle-radius': props.precision === 'exact' ? 7 : 26,
      'circle-color': '#38bdf8',
      'circle-opacity': props.precision === 'exact' ? 0.9 : 0.35,
      'circle-stroke-color': '#0ea5e9',
      'circle-stroke-width': 2,
    },
  })
}

onMounted(() => void initMap())

watch([published, () => props.precision], () => {
  if (map.value) refreshLayer(map.value)
})

onBeforeUnmount(() => {
  map.value?.remove()
  map.value = null
})
</script>

<template>
  <div>
    <p class="field-label">{{ t('metadata.locationPreview') }}</p>
    <div
      v-show="!failed"
      ref="container"
      class="h-56 w-full overflow-hidden rounded-lg border border-night-700"
      role="img"
      :aria-label="t('metadata.locationPreview')"
    />
    <p v-if="failed" class="muted mt-2 text-sm" role="status">
      {{ t('metadata.mapUnavailable') }}
    </p>
    <p v-else class="muted mt-2 text-sm">{{ t('metadata.locationPick') }}</p>
    <p class="mt-1 text-sm" data-testid="published-location">
      {{ t('metadata.publishedAs', { label }) }}
    </p>
    <p v-if="!location" class="muted mt-1 text-sm">{{ t('metadata.locationNone') }}</p>
    <p class="field-help">{{ t(`photo.precisionHelp.${precision}`) }}</p>
  </div>
</template>
