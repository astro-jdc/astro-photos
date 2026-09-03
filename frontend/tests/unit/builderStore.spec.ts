import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useBuilderStore } from '~/stores/builder'
import { clearNuxtState } from '../stubs/nuxt-app'
import type { PhotoSummary } from '~/types/domain'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function frame(id: string, overrides: Partial<PhotoSummary> = {}): PhotoSummary {
  return {
    id,
    title: `frame ${id}`,
    status: 'ready',
    owner: { id: 'u1', display_name: 'Ada' },
    license: 'CC-BY-NC-4.0',
    is_plate_solved: true,
    allow_derivatives_in_stacks: true,
    ...overrides,
  }
}

const plan = {
  selected: [frame('a'), frame('b')],
  blocked: [],
  resulting_license: 'CC-BY-NC-4.0',
  estimated_compute_seconds: 300,
  estimated_cost_usd: 0.2,
  estimated_queue_seconds: 30,
  pipeline: 'classical-stack-v1',
  uses_learned_model: false,
}

beforeEach(() => {
  clearNuxtState()
  setActivePinia(createPinia())
  localStorage.clear()
  vi.unstubAllGlobals()
})

describe('store builder', () => {
  it('añade, alterna y quita frames sin duplicar', () => {
    const builder = useBuilderStore()
    builder.add(frame('a'))
    builder.add(frame('a'))
    expect(builder.count).toBe(1)

    builder.toggle(frame('b'))
    expect(builder.count).toBe(2)
    builder.toggle(frame('b'))
    expect(builder.count).toBe(1)

    builder.remove('a')
    expect(builder.count).toBe(0)
  })

  it('no deja lanzar sin haber pasado por el preview', () => {
    const builder = useBuilderStore()
    builder.add(frame('a'))
    builder.add(frame('b'))
    expect(builder.canPreview).toBe(true)
    expect(builder.canLaunch).toBe(false)
  })

  it('habilita el lanzamiento solo tras un preview exitoso', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(plan)))
    const builder = useBuilderStore()
    builder.add(frame('a'))
    builder.add(frame('b'))

    await builder.runPreview()

    expect(builder.preview?.selected).toHaveLength(2)
    expect(builder.isStale).toBe(false)
    expect(builder.canLaunch).toBe(true)
  })

  it('cambiar la selección invalida el preview', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(plan)))
    const builder = useBuilderStore()
    builder.add(frame('a'))
    builder.add(frame('b'))
    await builder.runPreview()
    expect(builder.canLaunch).toBe(true)

    builder.add(frame('c'))
    expect(builder.preview).toBeNull()
    expect(builder.canLaunch).toBe(false)
  })

  it('cambiar de pipeline también invalida el preview', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(plan)))
    const builder = useBuilderStore()
    builder.add(frame('a'))
    builder.add(frame('b'))
    await builder.runPreview()

    builder.pipeline = 'drizzle-v1'
    expect(builder.canLaunch).toBe(false)
    expect(builder.usesLearnedModel).toBe(false)

    builder.pipeline = 'burst-sr-v1'
    expect(builder.usesLearnedModel).toBe(true)
  })

  it('un preview fallido deja el botón bloqueado', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse({ title: 'license_blocked', status: 422 }, 422)),
    )
    const builder = useBuilderStore()
    builder.add(frame('a'))
    builder.add(frame('b'))

    await builder.runPreview()

    expect(builder.preview).toBeNull()
    expect(builder.previewError?.status).toBe(422)
    expect(builder.canLaunch).toBe(false)
  })

  it('launch() no manda nada si no hay preview válido', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const builder = useBuilderStore()
    builder.add(frame('a'))
    builder.add(frame('b'))

    expect(await builder.launch()).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('dropBlocked() quita las fotos que el servidor rechazó', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({
          ...plan,
          selected: [frame('a')],
          blocked: [{ photo_id: 'b', reason: 'ND: no permite derivadas' }],
        }),
      ),
    )
    const builder = useBuilderStore()
    builder.add(frame('a'))
    builder.add(frame('b', { license: 'CC-BY-ND-4.0' }))
    await builder.runPreview()

    builder.dropBlocked()
    expect(builder.ids).toEqual(['a'])
  })

  it('la pista local de licencia detecta un ND antes de llamar al servidor', () => {
    const builder = useBuilderStore()
    builder.add(frame('a'))
    builder.add(frame('b', { license: 'CC-BY-ND-4.0' }))
    expect(builder.licenseHint.license).toBeNull()
    expect(builder.licenseHint.blocked).toEqual(['CC-BY-ND-4.0'])
  })

  it('persiste y restaura el carrito', () => {
    const builder = useBuilderStore()
    builder.add(frame('a'))
    builder.persist()

    const other = useBuilderStore()
    other.clear()
    other.restore()
    expect(other.ids).toEqual(['a'])
  })
})
