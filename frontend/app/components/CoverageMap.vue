<script setup lang="ts">
/**
 * Mapa de cobertura de un objeto: dónde y cuándo se ha fotografiado, y dónde
 * faltan aportaciones (`GET /objects/{id}/coverage`).
 *
 * Dos vistas complementarias:
 *  - MapLibre con la distribución geográfica de las tomas (respetando la
 *    precisión que cada autor autorizó a publicar).
 *  - Un heatmap tiempo × latitud, filtrable por franja de focal, que enseña
 *    las celdas vacías: eso es lo que falta.
 *
 * Si MapLibre o WebGL no están disponibles, la misma información se ofrece en
 * una tabla accesible.
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRuntimeConfig } from '#app'
import { round } from '~/lib/astro'
import type { CoverageCell, ObjectCoverage } from '~/types/domain'

const props = defineProps<{ coverage: ObjectCoverage | null; pending?: boolean }>()

const { t } = useI18n()
const config = useRuntimeConfig()

const mapContainer = ref<HTMLDivElement | null>(null)
const mapFailed = ref(false)
const map = shallowRef<{ remove: () => void } | null>(null)

const focalBins = computed(() => props.coverage?.focal_bins_mm ?? [])
const selectedFocal = ref<number | 'all'>('all')

const periods = computed(() => {
  const set = new Set<string>()
  for (const c of props.coverage?.cells ?? []) set.add(c.period_start)
  return [...set].sort()
})

const latBins = computed(() => {
  const set = new Set<number>()
  for (const c of props.coverage?.cells ?? []) set.add(c.lat_bin_deg)
  return [...set].sort((a, b) => b - a)
})

const grid = computed(() => {
  const byKey = new Map<string, number>()
  for (const cell of props.coverage?.cells ?? []) {
    if (selectedFocal.value !== 'all' && cell.focal_bin_mm !== selectedFocal.value) continue
    const key = `${cell.period_start}|${cell.lat_bin_deg}`
    byKey.set(key, (byKey.get(key) ?? 0) + cell.photo_count)
  }
  return byKey
})

const maxCount = computed(() => Math.max(1, ...[...grid.value.values()]))

function countAt(period: string, lat: number): number {
  return grid.value.get(`${period}|${lat}`) ?? 0
}

function cellStyle(count: number): Record<string, string> {
  if (count === 0) return { backgroundColor: 'transparent' }
  const ratio = Math.min(1, Math.log1p(count) / Math.log1p(maxCount.value))
  return { backgroundColor: `rgba(56, 189, 248, ${0.12 + ratio * 0.78})` }
}

function latLabel(lat: number): string {
  if (lat > 20) return `${round(lat, 0)}° N`
  if (lat < -20) return `${round(Math.abs(lat), 0)}° S`
  return `${round(lat, 0)}°`
}

function periodLabel(iso: string): string {
  return iso.slice(0, 7)
}

function cellsForFocal(focal: number): CoverageCell[] {
  return (props.coverage?.cells ?? []).filter((c) => c.focal_bin_mm === focal)
}

async function initMap() {
  if (!mapContainer.value || !props.coverage) return
  try {
    const maplibre = await import('maplibre-gl')
    const instance = new maplibre.Map({
      container: mapContainer.value,
      style: String(config.public.mapStyleUrl),
      center: [0, 20],
      zoom: 0.8,
      attributionControl: { compact: true },
    })
    instance.addControl(new maplibre.NavigationControl({ showCompass: false }), 'top-right')

    instance.on('load', () => {
      instance.addSource('coverage-sites', {
        type: 'geojson',
        data: {
          type: 'FeatureCollection',
          features: props.coverage!.sites.map((site) => ({
            type: 'Feature' as const,
            geometry: { type: 'Point' as const, coordinates: [site.lon, site.lat] },
            properties: { count: site.photo_count, precision: site.precision },
          })),
        },
      })
      instance.addLayer({
        id: 'coverage-sites-circles',
        type: 'circle',
        source: 'coverage-sites',
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['get', 'count'], 1, 4, 50, 12, 500, 22],
          'circle-color': '#38bdf8',
          'circle-opacity': 0.65,
          'circle-stroke-color': '#0ea5e9',
          'circle-stroke-width': 1,
        },
      })
    })

    map.value = instance
  } catch {
    mapFailed.value = true
  }
}

onMounted(() => {
  if (props.coverage) void initMap()
})

watch(
  () => props.coverage,
  (next) => {
    if (next && !map.value && !mapFailed.value) void initMap()
  },
)

onBeforeUnmount(() => {
  map.value?.remove()
  map.value = null
})
</script>

<template>
  <section class="surface p-4" aria-labelledby="coverage-title">
    <h2 id="coverage-title" class="text-lg font-semibold">{{ t('coverage.title') }}</h2>
    <p class="muted mt-1 text-sm">{{ t('coverage.description') }}</p>

    <p v-if="pending" class="muted mt-4 text-sm">{{ t('common.loading') }}</p>
    <p v-else-if="!coverage" class="muted mt-4 text-sm">{{ t('coverage.noData') }}</p>

    <template v-else>
      <div class="mt-6">
        <h3 class="font-medium">{{ t('coverage.mapTitle') }}</h3>
        <p class="muted mt-1 text-sm">{{ t('coverage.mapDescription') }}</p>
        <div
          v-show="!mapFailed"
          ref="mapContainer"
          class="mt-3 h-72 w-full overflow-hidden rounded-lg border border-night-700"
          role="img"
          :aria-label="t('coverage.mapTitle')"
        />
        <p v-if="mapFailed" class="mt-3 text-sm" role="status">
          {{ t('coverage.mapUnavailable') }}
        </p>

        <details class="mt-3">
          <summary class="cursor-pointer text-sm">{{ t('coverage.sitesTable') }}</summary>
          <div class="mt-2 overflow-x-auto">
            <table class="w-full min-w-[28rem] border-collapse text-sm">
              <thead>
                <tr class="border-b border-night-700 text-left">
                  <th scope="col" class="py-1 pr-3">{{ t('coverage.colLat') }}</th>
                  <th scope="col" class="py-1 pr-3">{{ t('coverage.colLon') }}</th>
                  <th scope="col" class="py-1 pr-3">{{ t('coverage.colPhotos') }}</th>
                  <th scope="col" class="py-1">{{ t('coverage.colPrecision') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(site, index) in coverage.sites"
                  :key="`${site.lat}-${site.lon}-${index}`"
                  class="border-b border-night-800/70"
                >
                  <td class="py-1 pr-3 font-mono">{{ round(site.lat, 2) }}</td>
                  <td class="py-1 pr-3 font-mono">{{ round(site.lon, 2) }}</td>
                  <td class="py-1 pr-3">{{ site.photo_count }}</td>
                  <td class="py-1">{{ t(`photo.precision.${site.precision}`) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>
      </div>

      <div class="mt-8">
        <h3 class="font-medium">{{ t('coverage.heatmapTitle') }}</h3>
        <p class="muted mt-1 text-sm">{{ t('coverage.heatmapDescription') }}</p>

        <div class="mt-3 flex flex-wrap items-center gap-2">
          <label class="field-label mb-0" for="coverage-focal">
            {{ t('coverage.focalFilter') }}
          </label>
          <select id="coverage-focal" v-model="selectedFocal" class="field-input w-auto py-1">
            <option value="all">{{ t('common.none') }}</option>
            <option v-for="focal in focalBins" :key="focal" :value="focal">
              {{ focal }} mm ({{ cellsForFocal(focal).length }})
            </option>
          </select>
        </div>

        <div class="mt-4 overflow-x-auto">
          <table class="border-collapse text-xs">
            <caption class="sr-only">
              {{ t('coverage.heatmapTitle') }}
            </caption>
            <thead>
              <tr>
                <th scope="col" class="p-1 text-left">{{ t('coverage.axisLat') }}</th>
                <th
                  v-for="period in periods"
                  :key="period"
                  scope="col"
                  class="p-1 text-left font-mono font-normal"
                >
                  {{ periodLabel(period) }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="lat in latBins" :key="lat">
                <th scope="row" class="whitespace-nowrap p-1 text-left font-mono font-normal">
                  {{ latLabel(lat) }}
                </th>
                <td
                  v-for="period in periods"
                  :key="`${lat}-${period}`"
                  class="h-6 w-8 border border-night-800 p-0 text-center"
                  :style="cellStyle(countAt(period, lat))"
                  :title="
                    countAt(period, lat) > 0
                      ? t('coverage.cellPhotos', { count: countAt(period, lat) })
                      : t('coverage.cellEmpty')
                  "
                >
                  <span class="sr-only">
                    {{ periodLabel(period) }}, {{ latLabel(lat) }}:
                    {{ t('coverage.cellPhotos', { count: countAt(period, lat) }) }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="mt-6">
        <h3 class="font-medium">{{ t('coverage.gapsTitle') }}</h3>
        <ul v-if="coverage.gaps.length > 0" class="mt-2 list-inside list-disc text-sm">
          <li v-for="(gap, index) in coverage.gaps" :key="index">{{ gap.detail }}</li>
        </ul>
        <p v-else class="muted mt-2 text-sm">{{ t('coverage.gapsEmpty') }}</p>
      </div>
    </template>
  </section>
</template>
