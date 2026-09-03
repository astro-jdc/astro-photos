/**
 * Utilidades de formato astronómico. Puras, sin dependencias: se prueban en
 * `tests/unit/astro.spec.ts`.
 */

function pad(n: number, width = 2): string {
  return String(Math.floor(n)).padStart(width, '0')
}

/** Normaliza a [0, 360). */
export function normalizeRaDeg(deg: number): number {
  const r = deg % 360
  return r < 0 ? r + 360 : r
}

export interface Hms {
  h: number
  m: number
  s: number
}

export interface Dms {
  sign: 1 | -1
  d: number
  m: number
  s: number
}

/** Ascensión recta en grados → horas/minutos/segundos. */
export function degToHms(deg: number, secondDecimals = 2): Hms {
  const hoursTotal = normalizeRaDeg(deg) / 15
  let h = Math.floor(hoursTotal)
  let m = Math.floor((hoursTotal - h) * 60)
  let s = round((hoursTotal - h - m / 60) * 3600, secondDecimals)
  // El redondeo puede desbordar: 59.999 s → 60 s.
  if (s >= 60) {
    s = 0
    m += 1
  }
  if (m >= 60) {
    m = 0
    h += 1
  }
  if (h >= 24) h -= 24
  return { h, m, s }
}

/** Declinación en grados → grados/arcmin/arcsec con signo. */
export function degToDms(deg: number, secondDecimals = 1): Dms {
  const sign: 1 | -1 = deg < 0 ? -1 : 1
  const abs = Math.abs(deg)
  let d = Math.floor(abs)
  let m = Math.floor((abs - d) * 60)
  let s = round((abs - d - m / 60) * 3600, secondDecimals)
  if (s >= 60) {
    s = 0
    m += 1
  }
  if (m >= 60) {
    m = 0
    d += 1
  }
  return { sign, d, m, s }
}

export function formatRa(deg: number, secondDecimals = 2): string {
  const { h, m, s } = degToHms(deg, secondDecimals)
  return `${pad(h)}h ${pad(m)}m ${s.toFixed(secondDecimals).padStart(secondDecimals + 3, '0')}s`
}

export function formatDec(deg: number, secondDecimals = 1): string {
  const { sign, d, m, s } = degToDms(deg, secondDecimals)
  const sym = sign < 0 ? '−' : '+'
  return `${sym}${pad(d)}° ${pad(m)}′ ${s.toFixed(secondDecimals).padStart(secondDecimals + 3, '0')}″`
}

/** "10h 42m 44.51s +41° 16′ 09.4″" */
export function formatCoords(raDeg: number, decDeg: number): string {
  return `${formatRa(raDeg)} ${formatDec(decDeg)}`
}

/** Ángulo pequeño en grados → la unidad legible más adecuada. */
export function formatAngle(deg: number): string {
  const abs = Math.abs(deg)
  if (abs >= 1) return `${round(deg, 3)}°`
  if (abs >= 1 / 60) return `${round(deg * 60, 2)}′`
  return `${round(deg * 3600, 2)}″`
}

/**
 * Límite de difracción (criterio de Rayleigh) en segundos de arco.
 * theta = 1.22 * lambda / D. Es el techo físico que la interfaz **no** debe
 * prometer superar (research §5).
 */
export function diffractionLimitArcsec(apertureMm: number, wavelengthNm = 550): number {
  if (!apertureMm || apertureMm <= 0) return Number.NaN
  const lambdaM = wavelengthNm * 1e-9
  const dM = apertureMm * 1e-3
  return ((1.22 * lambdaM) / dM) * (180 / Math.PI) * 3600
}

/** arcsec/píxel a partir de focal y tamaño de píxel: 206.265 * pitch / focal. */
export function pixelScaleArcsec(focalLengthMm: number, pixelPitchUm: number): number {
  if (!focalLengthMm || focalLengthMm <= 0) return Number.NaN
  return (206.265 * pixelPitchUm) / focalLengthMm
}

/** Apertura en mm a partir de focal y f/N. */
export function apertureMm(focalLengthMm: number, focalRatio: number): number {
  if (!focalRatio || focalRatio <= 0) return Number.NaN
  return focalLengthMm / focalRatio
}

export function formatExposure(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—'
  if (seconds < 1) return `1/${Math.round(1 / seconds)} s`
  if (seconds < 60) return `${round(seconds, 2)} s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  if (m < 60) return `${m} min ${pad(s)} s`
  const h = Math.floor(m / 60)
  return `${h} h ${pad(m % 60)} min`
}

export function formatBytes(bytes: number | null | undefined, digits = 1): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i += 1
  }
  return `${round(value, i === 0 ? 0 : digits)} ${units[i]}`
}

/** Desplazamiento UTC en minutos → "UTC+02:00". */
export function formatUtcOffset(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return 'UTC'
  const sign = minutes < 0 ? '-' : '+'
  const abs = Math.abs(minutes)
  return `UTC${sign}${pad(abs / 60)}:${pad(abs % 60)}`
}

export function round(value: number, digits = 2): number {
  const f = 10 ** digits
  return Math.round(value * f) / f
}

/** Etiqueta corta de calidad. `null` cuando el worker de QA aún no pasó. */
export type QualityTier = 'unrated' | 'low' | 'fair' | 'good' | 'excellent'

export function qualityTier(score: number | null | undefined): QualityTier {
  if (score === null || score === undefined || Number.isNaN(score)) return 'unrated'
  if (score < 0.35) return 'low'
  if (score < 0.6) return 'fair'
  if (score < 0.82) return 'good'
  return 'excellent'
}
