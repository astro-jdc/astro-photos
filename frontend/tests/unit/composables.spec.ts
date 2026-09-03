import { beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'
import { usePhotos, usePhoto } from '~/composables/usePhotos'
import { useObjectCoverage, useObjectSearch } from '~/composables/useObjects'
import { useLicenses } from '~/composables/useLicenses'
import { useReconstruction } from '~/composables/useReconstruction'
import { clearNuxtState } from '../stubs/nuxt-app'
import type { PhotoFilters } from '~/lib/photoQuery'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/** Ejecuta un composable dentro de un scope para que onScopeDispose funcione. */
function withScope<T>(fn: () => T): T {
  const scope = effectScope()
  const value = scope.run(fn)
  if (value === undefined) throw new Error('el composable no devolvió nada')
  return value
}

const photo = (id: string) => ({
  id,
  title: `foto ${id}`,
  status: 'ready',
  owner: { id: 'u1', display_name: 'Ada' },
  license: 'CC-BY-NC-4.0',
  is_plate_solved: true,
  allow_derivatives_in_stacks: true,
})

beforeEach(() => {
  clearNuxtState()
  vi.unstubAllGlobals()
})

describe('usePhotos', () => {
  it('carga la primera página y acumula con el cursor', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ items: [photo('a')], next_cursor: 'c1' }))
      .mockResolvedValueOnce(jsonResponse({ items: [photo('b')], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)

    const filters = ref<PhotoFilters>({ object: 'M31' })
    const list = withScope(() => usePhotos(filters))

    await list.refresh()
    expect(list.items.value.map((p) => p.id)).toEqual(['a'])
    expect(list.exhausted.value).toBe(false)

    await list.loadMore()
    expect(list.items.value.map((p) => p.id)).toEqual(['a', 'b'])
    expect(list.exhausted.value).toBe(true)

    const secondUrl = String(fetchMock.mock.calls[1]?.[0])
    expect(secondUrl).toContain('cursor=c1')
    expect(secondUrl).toContain('object=M31')
  })

  it('no vuelve a pedir cuando la lista está agotada', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)

    const list = withScope(() => usePhotos(ref<PhotoFilters>({})))
    await list.refresh()
    await list.loadMore()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(list.isEmpty.value).toBe(true)
  })

  it('guarda el problem+json como ApiError sin romper la lista', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse({ title: 'boom', status: 503 }, 503)),
    )

    const list = withScope(() => usePhotos(ref<PhotoFilters>({})))
    await list.refresh()

    expect(list.error.value?.status).toBe(503)
    expect(list.items.value).toEqual([])
  })
})

describe('usePhoto', () => {
  it('construye la URL de descarga sobre la baseURL de runtimeConfig', () => {
    const api = withScope(() => usePhoto())
    expect(api.downloadUrl('abc')).toBe('http://api.test/api/v1/photos/abc/download')
  })

  it('pide la ficha completa de forma anónima', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(photo('abc')))
    vi.stubGlobal('fetch', fetchMock)

    const api = withScope(() => usePhoto())
    await api.fetch('abc')

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://api.test/api/v1/photos/abc')
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined()
  })
})

describe('useObjectSearch', () => {
  it('descarta términos vacíos y devuelve resultados', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        items: [{ id: 'o1', catalog: 'M', catalog_number: '31', aliases: [] }],
        next_cursor: null,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const search = withScope(() => useObjectSearch())
    await search.search('   ')
    expect(fetchMock).not.toHaveBeenCalled()

    await search.search('M31')
    expect(search.results.value).toHaveLength(1)
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain('q=M31')
  })
})

describe('useObjectCoverage', () => {
  it('carga el mapa de cobertura del objeto', async () => {
    const coverage = {
      object_id: 'o1',
      cells: [
        { period_start: '2026-01-01', lat_bin_deg: 40, focal_bin_mm: 200, photo_count: 3 },
      ],
      sites: [],
      gaps: [{ kind: 'hemisphere', detail: 'faltan tomas del sur' }],
      period_bin: 'month',
      lat_bin_size_deg: 10,
      focal_bins_mm: [200],
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(coverage)))

    const c = withScope(() => useObjectCoverage())
    await c.load('o1')

    expect(c.coverage.value?.cells).toHaveLength(1)
    expect(c.pending.value).toBe(false)
  })
})

describe('useLicenses', () => {
  it('arranca con el catálogo local aunque el servidor no conteste', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))

    const licenses = withScope(() => useLicenses())
    expect(licenses.catalog.value).toHaveLength(8)
    expect(licenses.defaultLicense).toBe('CC-BY-NC-4.0')

    await licenses.load()
    expect(licenses.error.value?.status).toBe(0)
    expect(licenses.catalog.value).toHaveLength(8)
  })

  it('sobrescribe los flags con lo que diga el servidor', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse([
          {
            code: 'CC-BY-NC-4.0',
            name: 'x',
            version: '4.0',
            url: 'https://example.test',
            allows_commercial: false,
            allows_derivatives: true,
            requires_attribution: true,
            requires_sharealike: false,
            restrictiveness: 9,
            spdx_id: 'CC-BY-NC-4.0',
          },
        ]),
      ),
    )

    const licenses = withScope(() => useLicenses())
    await licenses.load()
    await nextTick()

    const nc = licenses.catalog.value.find((l) => l.code === 'CC-BY-NC-4.0')
    expect(nc?.restrictiveness).toBe(9)
    expect(nc?.url).toBe('https://example.test')
  })

  it('resolve() llama a POST /licenses/resolve', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ resulting_license: 'CC-BY-NC-4.0', blocked: [] }))
    vi.stubGlobal('fetch', fetchMock)

    const licenses = withScope(() => useLicenses())
    const resolution = await licenses.resolve(['a', 'b'])

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://api.test/api/v1/licenses/resolve')
    expect(init.method).toBe('POST')
    expect(resolution.resulting_license).toBe('CC-BY-NC-4.0')
  })
})

describe('useReconstruction', () => {
  it('preview() no encola nada y devuelve el plan', async () => {
    const plan = {
      selected: [photo('a'), photo('b')],
      blocked: [{ photo_id: 'c', reason: 'ND' }],
      resulting_license: 'CC-BY-NC-4.0',
      estimated_compute_seconds: 600,
      estimated_cost_usd: 0.42,
      estimated_queue_seconds: 60,
      pipeline: 'classical-stack-v1',
      uses_learned_model: false,
    }
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(plan))
    vi.stubGlobal('fetch', fetchMock)

    const recon = withScope(() => useReconstruction())
    const preview = await recon.preview({ pipeline: 'classical-stack-v1', photo_ids: ['a', 'b'] })

    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('http://api.test/api/v1/reconstructions/preview')
    expect(preview.selected).toHaveLength(2)
    expect(preview.blocked).toHaveLength(1)
  })

  it('load() rellena el job y el progreso', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({
          id: 'r1',
          status: 'running',
          progress: 0.42,
          pipeline: 'drizzle-v1',
          pipeline_version: 'abc',
          params: {},
          input_count: 12,
          license: 'CC-BY-NC-4.0',
          created_at: '2026-01-01T00:00:00Z',
          uses_learned_model: false,
          requested_by: { id: 'u1', display_name: 'Ada' },
        }),
      ),
    )

    const recon = withScope(() => useReconstruction())
    await recon.load('r1')

    expect(recon.job.value?.status).toBe('running')
    expect(recon.progress.value).toBeCloseTo(0.42)
  })
})
