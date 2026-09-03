<script setup lang="ts">
/**
 * Procedencia: cada foto que entró en una reconstrucción, con su peso y la
 * licencia vigente en ese momento (`reconstruction_inputs.snapshot_license`).
 * Esta tabla no se reescribe nunca aunque el autor cambie la licencia después.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { round } from '~/lib/astro'
import type { ReconstructionInput } from '~/types/domain'

const props = withDefaults(
  defineProps<{
    inputs: ReconstructionInput[]
    attributionUrl?: string | null
    provenanceUrl?: string | null
  }>(),
  { attributionUrl: null, provenanceUrl: null },
)

const { t } = useI18n()

const sorted = computed(() => [...props.inputs].sort((a, b) => b.weight - a.weight))
</script>

<template>
  <section class="surface p-4">
    <h2 class="text-lg font-semibold">{{ t('provenance.title') }}</h2>
    <p class="muted mt-1 text-sm">{{ t('provenance.description') }}</p>

    <p v-if="sorted.length === 0" class="muted mt-4 text-sm">{{ t('provenance.empty') }}</p>

    <div v-else class="mt-4 overflow-x-auto">
      <table class="w-full min-w-[46rem] border-collapse text-sm">
        <caption class="sr-only">
          {{ t('provenance.caption') }}
        </caption>
        <thead>
          <tr class="border-b border-night-700 text-left">
            <th scope="col" class="py-2 pr-3 font-medium">{{ t('provenance.colPhoto') }}</th>
            <th scope="col" class="py-2 pr-3 font-medium">{{ t('provenance.colAuthor') }}</th>
            <th scope="col" class="py-2 pr-3 font-medium">{{ t('provenance.colWeight') }}</th>
            <th scope="col" class="py-2 pr-3 font-medium">{{ t('provenance.colLicense') }}</th>
            <th scope="col" class="py-2 pr-3 font-medium">{{ t('provenance.colAlignment') }}</th>
            <th scope="col" class="py-2 font-medium">{{ t('provenance.colStatus') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in sorted" :key="row.photo_id" class="border-b border-night-800/70">
            <td class="py-2 pr-3">
              <!--
                `ReconstructionInputOut` no anida la foto: trae el id y la
                autoría congelada en el momento del uso, que es justamente lo
                que la procedencia tiene que enseñar (un cambio posterior de
                nombre no reescribe una reconstrucción publicada).
              -->
              <NuxtLink :to="`/photos/${row.photo_id}`" class="font-mono hover:underline">
                {{ row.photo_id.slice(0, 8) }}
              </NuxtLink>
            </td>
            <td class="py-2 pr-3">
              {{ row.snapshot_attribution_name || t('common.unknown') }}
            </td>
            <td class="py-2 pr-3 font-mono">{{ round(row.weight, 3) }}</td>
            <td class="py-2 pr-3"><LicenseBadge :code="row.snapshot_license" /></td>
            <td class="py-2 pr-3 font-mono">
              {{ row.alignment_rms_px === null || row.alignment_rms_px === undefined
                ? t('common.notAvailable')
                : `${round(row.alignment_rms_px, 3)} px` }}
            </td>
            <td class="py-2">
              <span v-if="row.was_rejected" class="chip border-rose-500/60 text-rose-300">
                {{ t('provenance.rejected') }}
              </span>
              <span v-else class="chip border-emerald-500/60 text-emerald-300">
                {{ t('provenance.accepted') }}
              </span>
              <span v-if="row.was_rejected && row.rejection_reason" class="muted block text-xs">
                {{ t('provenance.reason') }}: {{ row.rejection_reason }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p v-if="attributionUrl || provenanceUrl" class="mt-4 flex flex-wrap gap-3 text-sm">
      <a
        v-if="attributionUrl"
        :href="attributionUrl"
        class="text-sky-300 underline"
        target="_blank"
        rel="noopener noreferrer"
      >
        {{ t('reconstruction.attribution') }}
      </a>
      <a
        v-if="provenanceUrl"
        :href="provenanceUrl"
        class="text-sky-300 underline"
        target="_blank"
        rel="noopener noreferrer"
      >
        {{ t('reconstruction.provenance') }}
      </a>
    </p>
  </section>
</template>
