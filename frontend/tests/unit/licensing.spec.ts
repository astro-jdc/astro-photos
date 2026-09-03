import { describe, expect, it } from 'vitest'
import {
  commercialLicenseCodes,
  DEFAULT_LICENSE,
  forbidsStackDerivatives,
  LICENSES,
  licenseFacts,
  resolveOutputLicenseHint,
} from '~/lib/licensing'

describe('catálogo de licencias', () => {
  it('tiene las 8 licencias de docs/licensing.md en orden de restrictividad', () => {
    expect(LICENSES).toHaveLength(8)
    expect(LICENSES.map((l) => l.code)).toEqual([
      'CC0-1.0',
      'CC-BY-4.0',
      'CC-BY-SA-4.0',
      'CC-BY-NC-4.0',
      'CC-BY-NC-SA-4.0',
      'CC-BY-ND-4.0',
      'CC-BY-NC-ND-4.0',
      'ARR',
    ])
    expect(LICENSES.map((l) => l.restrictiveness)).toEqual([0, 1, 2, 3, 4, 5, 6, 7])
  })

  it('la preselección es CC-BY-NC-4.0', () => {
    expect(DEFAULT_LICENSE).toBe('CC-BY-NC-4.0')
    expect(licenseFacts(DEFAULT_LICENSE).allowsCommercial).toBe(false)
    expect(licenseFacts(DEFAULT_LICENSE).allowsDerivatives).toBe(true)
  })

  it('ND y ARR prohíben el uso como frame en un apilado', () => {
    expect(forbidsStackDerivatives('CC-BY-ND-4.0')).toBe(true)
    expect(forbidsStackDerivatives('CC-BY-NC-ND-4.0')).toBe(true)
    expect(forbidsStackDerivatives('ARR')).toBe(true)
    expect(forbidsStackDerivatives('CC-BY-NC-4.0')).toBe(false)
    expect(forbidsStackDerivatives('CC0-1.0')).toBe(false)
  })

  it('lista las licencias con uso comercial', () => {
    expect(commercialLicenseCodes()).toEqual([
      'CC0-1.0',
      'CC-BY-4.0',
      'CC-BY-SA-4.0',
      'CC-BY-ND-4.0',
    ])
  })

  it('lanza con un código desconocido', () => {
    // @ts-expect-error probamos deliberadamente un código fuera del enum
    expect(() => licenseFacts('CC-BY-9.9')).toThrow()
  })
})

describe('resolveOutputLicenseHint', () => {
  const ok = (license: Parameters<typeof licenseFacts>[0]) => ({
    license,
    allowDerivativesInStacks: true,
  })

  it('CC0 solo sale si todas las entradas son CC0', () => {
    expect(resolveOutputLicenseHint([ok('CC0-1.0'), ok('CC0-1.0')]).license).toBe('CC0-1.0')
    expect(resolveOutputLicenseHint([ok('CC0-1.0'), ok('CC-BY-4.0')]).license).toBe('CC-BY-4.0')
  })

  it('NoComercial es contagioso', () => {
    expect(resolveOutputLicenseHint([ok('CC-BY-4.0'), ok('CC-BY-NC-4.0')]).license).toBe(
      'CC-BY-NC-4.0',
    )
  })

  it('ShareAlike es contagioso', () => {
    expect(resolveOutputLicenseHint([ok('CC-BY-4.0'), ok('CC-BY-SA-4.0')]).license).toBe(
      'CC-BY-SA-4.0',
    )
  })

  it('NC y SA juntos dan CC-BY-NC-SA', () => {
    expect(resolveOutputLicenseHint([ok('CC-BY-NC-4.0'), ok('CC-BY-SA-4.0')]).license).toBe(
      'CC-BY-NC-SA-4.0',
    )
  })

  it('un ND bloquea el trabajo en vez de degradar la salida', () => {
    const result = resolveOutputLicenseHint([ok('CC-BY-4.0'), ok('CC-BY-ND-4.0')])
    expect(result.license).toBeNull()
    expect(result.blocked).toEqual(['CC-BY-ND-4.0'])
  })

  it('allow_derivatives_in_stacks=false bloquea aunque la licencia lo permita', () => {
    const result = resolveOutputLicenseHint([
      ok('CC-BY-4.0'),
      { license: 'CC-BY-4.0', allowDerivativesInStacks: false },
    ])
    expect(result.license).toBeNull()
    expect(result.blocked).toHaveLength(1)
  })

  it('sin entradas no propone licencia', () => {
    expect(resolveOutputLicenseHint([])).toEqual({ license: null, blocked: [] })
  })
})
