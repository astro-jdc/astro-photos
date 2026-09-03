/**
 * Traducción entre el estado de los filtros de `/explore` y los parámetros de
 * `GET /photos` descritos en docs/api.md. Puro, para poder probarlo.
 */
import type { QueryParams } from './apiClient'
import type { LicenseCode } from '~/types/domain'

export type PhotoSort = 'quality' | 'recent' | 'nearest'
export type UsableFor = 'commercial' | 'any'

export interface PhotoFilters {
  /** alias o id del objeto */
  object?: string | null
  /** cono en el cielo, grados */
  ra?: number | null
  dec?: number | null
  radius?: number | null
  /** cerca de una posición en la Tierra */
  nearLat?: number | null
  nearLon?: number | null
  km?: number | null
  from?: string | null
  to?: string | null
  minFocal?: number | null
  maxFocal?: number | null
  filter?: string | null
  license?: LicenseCode[]
  usableFor?: UsableFor | null
  minQuality?: number | null
  tracked?: boolean | null
  sort?: PhotoSort | null
  limit?: number
  cursor?: string | null
}

export const DEFAULT_PAGE_SIZE = 48

export function toQuery(filters: PhotoFilters): QueryParams {
  const q: QueryParams = {}

  if (filters.object) q.object = filters.object

  if (isNum(filters.ra) && isNum(filters.dec)) {
    q.ra = filters.ra
    q.dec = filters.dec
    q.radius = isNum(filters.radius) ? filters.radius : 2
  }

  if (isNum(filters.nearLat) && isNum(filters.nearLon)) {
    q.near = `${filters.nearLat},${filters.nearLon}`
    if (isNum(filters.km)) q.km = filters.km
  }

  if (filters.from) q.from = filters.from
  if (filters.to) q.to = filters.to
  if (isNum(filters.minFocal)) q.min_focal = filters.minFocal
  if (isNum(filters.maxFocal)) q.max_focal = filters.maxFocal
  if (filters.filter) q.filter = filters.filter
  if (filters.license && filters.license.length > 0) q.license = filters.license
  if (filters.usableFor && filters.usableFor !== 'any') q.usable_for = filters.usableFor
  if (isNum(filters.minQuality) && filters.minQuality > 0) q.min_quality = filters.minQuality
  if (filters.tracked === true || filters.tracked === false) q.tracked = filters.tracked
  if (filters.sort) q.sort = filters.sort

  q.limit = filters.limit ?? DEFAULT_PAGE_SIZE
  if (filters.cursor) q.cursor = filters.cursor

  return q
}

/** Filtros ↔ query string de la URL, para que `/explore` sea compartible. */
export function toRouteQuery(filters: PhotoFilters): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(toQuery(filters))) {
    if (k === 'limit' || k === 'cursor') continue
    if (v === null || v === undefined || v === '') continue
    out[k] = Array.isArray(v) ? v.join(',') : String(v)
  }
  return out
}

export function fromRouteQuery(query: Record<string, string | string[] | undefined>): PhotoFilters {
  const one = (k: string): string | null => {
    const v = query[k]
    if (Array.isArray(v)) return v[0] ?? null
    return v ?? null
  }
  const num = (k: string): number | null => {
    const v = one(k)
    if (v === null || v === '') return null
    const n = Number(v)
    return Number.isFinite(n) ? n : null
  }

  const near = one('near')
  const [nearLat, nearLon] = near
    ? near.split(',').map((s) => {
        const n = Number(s)
        return Number.isFinite(n) ? n : null
      })
    : [null, null]

  const trackedRaw = one('tracked')
  const sortRaw = one('sort')
  const usableRaw = one('usable_for')

  return {
    object: one('object'),
    ra: num('ra'),
    dec: num('dec'),
    radius: num('radius'),
    nearLat: nearLat ?? null,
    nearLon: nearLon ?? null,
    km: num('km'),
    from: one('from'),
    to: one('to'),
    minFocal: num('min_focal'),
    maxFocal: num('max_focal'),
    filter: one('filter'),
    license: (one('license')?.split(',').filter(Boolean) ?? []) as LicenseCode[],
    usableFor: usableRaw === 'commercial' ? 'commercial' : null,
    minQuality: num('min_quality'),
    tracked: trackedRaw === null ? null : trackedRaw === 'true',
    sort: sortRaw === 'quality' || sortRaw === 'recent' || sortRaw === 'nearest' ? sortRaw : null,
  }
}

export function countActiveFilters(filters: PhotoFilters): number {
  return Object.keys(toRouteQuery(filters)).length
}

function isNum(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}
