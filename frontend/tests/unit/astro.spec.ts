import { describe, expect, it } from 'vitest'
import {
  apertureMm,
  degToDms,
  degToHms,
  diffractionLimitArcsec,
  formatAngle,
  formatBytes,
  formatDec,
  formatExposure,
  formatRa,
  formatUtcOffset,
  normalizeRaDeg,
  pixelScaleArcsec,
  qualityTier,
  round,
} from '~/lib/astro'

describe('normalizeRaDeg', () => {
  it('lleva cualquier ángulo a [0, 360)', () => {
    expect(normalizeRaDeg(-10)).toBeCloseTo(350)
    expect(normalizeRaDeg(370)).toBeCloseTo(10)
    expect(normalizeRaDeg(0)).toBe(0)
  })
})

describe('degToHms', () => {
  it('convierte la AR de M31 (10.6847°) a 00h 42m 44.33s', () => {
    const { h, m, s } = degToHms(10.6847)
    expect(h).toBe(0)
    expect(m).toBe(42)
    expect(s).toBeCloseTo(44.33, 2)
  })

  it('convierte 180° a 12h exactas', () => {
    expect(degToHms(180)).toEqual({ h: 12, m: 0, s: 0 })
  })

  it('no desborda al redondear los segundos', () => {
    // 15° - un pelo: 0h 59m 59.999...s debe subir a 1h 00m 00.00s
    const { h, m, s } = degToHms(14.9999999)
    expect(h).toBe(1)
    expect(m).toBe(0)
    expect(s).toBe(0)
  })
})

describe('degToDms', () => {
  it('convierte la Dec de M31 (41.269°) a +41° 16′ 08.4″', () => {
    const { sign, d, m, s } = degToDms(41.269)
    expect(sign).toBe(1)
    expect(d).toBe(41)
    expect(m).toBe(16)
    expect(s).toBeCloseTo(8.4, 1)
  })

  it('conserva el signo en declinaciones negativas', () => {
    const { sign, d, m } = degToDms(-29.00778)
    expect(sign).toBe(-1)
    expect(d).toBe(29)
    expect(m).toBe(0)
  })
})

describe('formatRa / formatDec', () => {
  it('formatea M31 en sexagesimal', () => {
    expect(formatRa(10.6847)).toBe('00h 42m 44.33s')
    expect(formatDec(41.269)).toBe('+41° 16′ 08.4″')
  })

  it('usa el signo menos tipográfico para declinaciones negativas', () => {
    expect(formatDec(-5.5)).toBe('−05° 30′ 00.0″')
  })
})

describe('formatAngle', () => {
  it('elige grados, minutos o segundos de arco', () => {
    expect(formatAngle(2.5)).toBe('2.5°')
    expect(formatAngle(0.5)).toBe('30′')
    expect(formatAngle(0.001)).toBe('3.6″')
  })
})

describe('límites ópticos', () => {
  it('calcula el límite de difracción de una apertura de 100 mm', () => {
    // 1.22 * 550 nm / 100 mm ≈ 1.385 arcsec
    expect(diffractionLimitArcsec(100)).toBeCloseTo(1.38, 1)
  })

  it('devuelve NaN sin apertura conocida', () => {
    expect(Number.isNaN(diffractionLimitArcsec(0))).toBe(true)
  })

  it('deriva la apertura de focal y f/N', () => {
    expect(apertureMm(600, 6)).toBe(100)
  })

  it('calcula arcsec/píxel', () => {
    // 50 mm con píxeles de 4 um → ~16.5 arcsec/px (muy submuestreado)
    expect(pixelScaleArcsec(50, 4)).toBeCloseTo(16.5, 1)
  })
})

describe('formateadores', () => {
  it('formatea exposiciones', () => {
    expect(formatExposure(null)).toBe('—')
    expect(formatExposure(0.004)).toBe('1/250 s')
    expect(formatExposure(30)).toBe('30 s')
    expect(formatExposure(305)).toBe('5 min 05 s')
    expect(formatExposure(7200)).toBe('2 h 00 min')
  })

  it('formatea bytes en unidades binarias', () => {
    expect(formatBytes(null)).toBe('—')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(21474836480)).toBe('20 GiB')
  })

  it('formatea el desplazamiento UTC', () => {
    expect(formatUtcOffset(120)).toBe('UTC+02:00')
    expect(formatUtcOffset(-330)).toBe('UTC-05:30')
    expect(formatUtcOffset(null)).toBe('UTC')
  })

  it('redondea a los decimales pedidos', () => {
    expect(round(1.23456, 2)).toBe(1.23)
    expect(round(1.23456, 0)).toBe(1)
  })
})

describe('qualityTier', () => {
  it('clasifica la puntuación de calidad', () => {
    expect(qualityTier(null)).toBe('unrated')
    expect(qualityTier(undefined)).toBe('unrated')
    expect(qualityTier(0.1)).toBe('low')
    expect(qualityTier(0.5)).toBe('fair')
    expect(qualityTier(0.7)).toBe('good')
    expect(qualityTier(0.95)).toBe('excellent')
  })
})
