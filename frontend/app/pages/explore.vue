<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { useSeoMeta } from '#app'
import { usePhotos } from '~/composables/usePhotos'
import {
  fromRouteQuery,
  toRouteQuery,
  type PhotoFilters,
  type PhotoSort,
} from '~/lib/photoQuery'
import { LICENSES } from '~/lib/licensing'
import type { LicenseCode } from '~/types/domain'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

const filters = ref<PhotoFilters>(
  fromRouteQuery(route.query as Record<string, string | string[] | undefined>),
)

const { items, pending, error, exhausted, isEmpty, refresh, loadMore } = usePhotos(filters)

const sentinel = ref<HTMLDivElement | null>(null)
let observer: IntersectionObserver | null = null

const FILTER_NAMES = ['none', 'UV/IR-cut', 'L-eNhance', 'Ha', 'OIII', 'SII', 'L', 'R', 'G', 'B']
const SORTS: PhotoSort[] = ['quality', 'recent', 'nearest']

let debounce: ReturnType<typeof setTimeout> | null = null

watch(
  filters,
  () => {
    if (debounce) clearTimeout(debounce)
    debounce = setTimeout(() => {
      void router.replace({ query: toRouteQuery(filters.value) })
      void refresh()
    }, 300)
  },
  { deep: true },
)

function toggleLicense(code: LicenseCode) {
  const current = filters.value.license ?? []
  filters.value = {
    ...filters.value,
    license: current.includes(code) ? current.filter((c) => c !== code) : [...current, code],
  }
}

function reset() {
  filters.value = { license: [] }
}

function useMyLocation() {
  if (typeof navigator === 'undefined' || !navigator.geolocation) return
  navigator.geolocation.getCurrentPosition((position) => {
    filters.value = {
      ...filters.value,
      nearLat: position.coords.latitude,
      nearLon: position.coords.longitude,
      km: filters.value.km ?? 100,
    }
  })
}

onMounted(() => {
  void refresh()
  if (typeof IntersectionObserver === 'undefined' || !sentinel.value) return
  observer = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) void loadMore()
  })
  observer.observe(sentinel.value)
})

onBeforeUnmount(() => observer?.disconnect())

useSeoMeta({ title: () => t('explore.title'), description: () => t('explore.subtitle') })
</script>

<template>
  <div class="grid gap-6 lg:grid-cols-[18rem_1fr]">
    <aside class="surface h-fit p-4" aria-labelledby="filters-title">
      <div class="flex items-center gap-2">
        <h2 id="filters-title" class="text-lg font-semibold">{{ t('explore.filters.title') }}</h2>
        <button type="button" class="btn-ghost ml-auto py-1 text-xs" @click="reset">
          {{ t('explore.filters.reset') }}
        </button>
      </div>

      <div class="mt-4 grid gap-4">
        <div>
          <label class="field-label" for="f-object">{{ t('explore.filters.object') }}</label>
          <input
            id="f-object"
            v-model="filters.object"
            class="field-input"
            type="search"
            :placeholder="t('explore.searchPlaceholder')"
          />
        </div>

        <fieldset>
          <legend class="field-label">{{ t('explore.filters.cone') }}</legend>
          <div class="grid grid-cols-3 gap-2">
            <input
              v-model.number="filters.ra"
              class="field-input"
              type="number"
              step="0.001"
              :aria-label="t('explore.filters.ra')"
              :placeholder="t('explore.filters.ra')"
            />
            <input
              v-model.number="filters.dec"
              class="field-input"
              type="number"
              step="0.001"
              :aria-label="t('explore.filters.dec')"
              :placeholder="t('explore.filters.dec')"
            />
            <input
              v-model.number="filters.radius"
              class="field-input"
              type="number"
              step="0.1"
              :aria-label="t('explore.filters.radius')"
              :placeholder="t('explore.filters.radius')"
            />
          </div>
          <p class="field-help">{{ t('explore.filters.coneHelp') }}</p>
        </fieldset>

        <fieldset>
          <legend class="field-label">{{ t('explore.filters.near') }}</legend>
          <div class="grid grid-cols-3 gap-2">
            <input
              v-model.number="filters.nearLat"
              class="field-input"
              type="number"
              step="0.0001"
              :aria-label="t('explore.filters.lat')"
              :placeholder="t('explore.filters.lat')"
            />
            <input
              v-model.number="filters.nearLon"
              class="field-input"
              type="number"
              step="0.0001"
              :aria-label="t('explore.filters.lon')"
              :placeholder="t('explore.filters.lon')"
            />
            <input
              v-model.number="filters.km"
              class="field-input"
              type="number"
              step="1"
              :aria-label="t('explore.filters.km')"
              :placeholder="t('explore.filters.km')"
            />
          </div>
          <button type="button" class="btn-secondary mt-2 w-full py-1 text-xs" @click="useMyLocation">
            {{ t('explore.filters.useMyLocation') }}
          </button>
          <p class="field-help">{{ t('explore.filters.nearHelp') }}</p>
        </fieldset>

        <fieldset>
          <legend class="field-label">{{ t('explore.filters.dates') }}</legend>
          <div class="grid grid-cols-2 gap-2">
            <input
              v-model="filters.from"
              class="field-input"
              type="date"
              :aria-label="t('explore.filters.from')"
            />
            <input
              v-model="filters.to"
              class="field-input"
              type="date"
              :aria-label="t('explore.filters.to')"
            />
          </div>
        </fieldset>

        <fieldset>
          <legend class="field-label">{{ t('explore.filters.focal') }}</legend>
          <div class="grid grid-cols-2 gap-2">
            <input
              v-model.number="filters.minFocal"
              class="field-input"
              type="number"
              min="0"
              :aria-label="t('explore.filters.minFocal')"
              :placeholder="t('explore.filters.minFocal')"
            />
            <input
              v-model.number="filters.maxFocal"
              class="field-input"
              type="number"
              min="0"
              :aria-label="t('explore.filters.maxFocal')"
              :placeholder="t('explore.filters.maxFocal')"
            />
          </div>
        </fieldset>

        <div>
          <label class="field-label" for="f-filter">{{ t('explore.filters.filter') }}</label>
          <select id="f-filter" v-model="filters.filter" class="field-input">
            <option :value="null">{{ t('common.none') }}</option>
            <option v-for="name in FILTER_NAMES" :key="name" :value="name">{{ name }}</option>
          </select>
        </div>

        <fieldset>
          <legend class="field-label">{{ t('explore.filters.license') }}</legend>
          <ul class="grid gap-1">
            <li v-for="license in LICENSES" :key="license.code">
              <label class="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  :checked="(filters.license ?? []).includes(license.code)"
                  @change="toggleLicense(license.code)"
                />
                <span class="font-mono text-xs">{{ license.code }}</span>
              </label>
            </li>
          </ul>
          <label class="mt-2 flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              :checked="filters.usableFor === 'commercial'"
              @change="
                filters.usableFor = ($event.target as HTMLInputElement).checked
                  ? 'commercial'
                  : null
              "
            />
            {{ t('explore.filters.commercialOnly') }}
          </label>
          <p class="field-help">{{ t('explore.filters.licenseHelp') }}</p>
        </fieldset>

        <div>
          <label class="field-label" for="f-quality">
            {{ t('explore.filters.minQuality') }}
            <span class="muted">{{ (filters.minQuality ?? 0).toFixed(2) }}</span>
          </label>
          <input
            id="f-quality"
            v-model.number="filters.minQuality"
            class="w-full"
            type="range"
            min="0"
            max="1"
            step="0.05"
          />
        </div>

        <div>
          <label class="field-label" for="f-tracked">{{ t('explore.filters.tracked') }}</label>
          <select
            id="f-tracked"
            class="field-input"
            :value="filters.tracked === null || filters.tracked === undefined ? '' : String(filters.tracked)"
            @change="
              filters.tracked =
                ($event.target as HTMLSelectElement).value === ''
                  ? null
                  : ($event.target as HTMLSelectElement).value === 'true'
            "
          >
            <option value="">{{ t('explore.filters.trackedAny') }}</option>
            <option value="true">{{ t('explore.filters.trackedYes') }}</option>
            <option value="false">{{ t('explore.filters.trackedNo') }}</option>
          </select>
        </div>

        <div>
          <label class="field-label" for="f-sort">{{ t('explore.filters.sort') }}</label>
          <select id="f-sort" v-model="filters.sort" class="field-input">
            <option :value="null">{{ t('common.none') }}</option>
            <option v-for="sort in SORTS" :key="sort" :value="sort">
              {{ t(`explore.filters.sort${sort.charAt(0).toUpperCase() + sort.slice(1)}`) }}
            </option>
          </select>
        </div>
      </div>
    </aside>

    <section aria-labelledby="explore-title">
      <h1 id="explore-title" class="text-2xl font-semibold">{{ t('explore.title') }}</h1>
      <p class="muted mt-1">{{ t('explore.subtitle') }}</p>

      <p
        v-if="error"
        class="mt-4 rounded-lg border border-rose-500/50 bg-rose-500/10 px-3 py-2 text-sm"
        role="alert"
      >
        {{ error.title }}<template v-if="error.detail"> — {{ error.detail }}</template>
        <button type="button" class="btn-ghost ml-2 py-0.5 text-xs" @click="refresh()">
          {{ t('common.retry') }}
        </button>
      </p>

      <p v-if="items.length > 0" class="muted mt-4 text-sm" aria-live="polite">
        {{ t('explore.resultsCount', { count: items.length }) }}
      </p>

      <div class="mt-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <PhotoCard v-for="photo in items" :key="photo.id" :photo="photo" />
      </div>

      <p v-if="isEmpty && !error" class="muted mt-8 text-sm">{{ t('explore.empty') }}</p>
      <p v-if="pending" class="muted mt-6 text-sm" role="status">{{ t('common.loading') }}</p>
      <p v-else-if="exhausted && items.length > 0" class="muted mt-6 text-sm">
        {{ t('explore.endOfResults') }}
      </p>

      <div ref="sentinel" class="h-8" aria-hidden="true" />

      <button
        v-if="!exhausted && items.length > 0"
        type="button"
        class="btn-secondary mt-2"
        :disabled="pending"
        @click="loadMore()"
      >
        {{ t('explore.loadMore') }}
      </button>
    </section>
  </div>
</template>
