import { describe, expect, it } from 'vitest'
import {
  countActiveFilters,
  DEFAULT_PAGE_SIZE,
  fromRouteQuery,
  toQuery,
  toRouteQuery,
} from '~/lib/photoQuery'

describe('toQuery', () => {
  it('traduce los filtros a los parámetros de docs/api.md', () => {
    expect(
      toQuery({
        object: 'M31',
        ra: 10.68,
        dec: 41.27,
        radius: 2,
        nearLat: 28.3,
        nearLon: -16.51,
        km: 50,
        from: '2026-01-01',
        to: '2026-03-01',
        minFocal: 200,
        maxFocal: 800,
        filter: 'Ha',
        license: ['CC-BY-4.0', 'CC0-1.0'],
        usableFor: 'commercial',
        minQuality: 0.6,
        tracked: true,
        sort: 'quality',
      }),
    ).toEqual({
      object: 'M31',
      ra: 10.68,
      dec: 41.27,
      radius: 2,
      near: '28.3,-16.51',
      km: 50,
      from: '2026-01-01',
      to: '2026-03-01',
      min_focal: 200,
      max_focal: 800,
      filter: 'Ha',
      license: ['CC-BY-4.0', 'CC0-1.0'],
      usable_for: 'commercial',
      min_quality: 0.6,
      tracked: true,
      sort: 'quality',
      limit: DEFAULT_PAGE_SIZE,
    })
  })

  it('pone radio 2° por defecto cuando hay cono sin radio', () => {
    expect(toQuery({ ra: 10, dec: 41 }).radius).toBe(2)
  })

  it('ignora un cono incompleto', () => {
    const q = toQuery({ ra: 10 })
    expect(q.ra).toBeUndefined()
    expect(q.radius).toBeUndefined()
  })

  it('deja pasar tracked=false', () => {
    expect(toQuery({ tracked: false }).tracked).toBe(false)
  })

  it('omite min_quality cuando es 0', () => {
    expect(toQuery({ minQuality: 0 }).min_quality).toBeUndefined()
  })
})

describe('ida y vuelta con la query de la ruta', () => {
  it('conserva los filtros al serializar y volver a leer', () => {
    const filters = {
      object: 'NGC7000',
      nearLat: 40.4,
      nearLon: -3.7,
      km: 25,
      minFocal: 135,
      filter: 'OIII',
      license: ['CC-BY-SA-4.0' as const],
      minQuality: 0.5,
      tracked: false,
      sort: 'recent' as const,
    }
    const routeQuery = toRouteQuery(filters)
    const parsed = fromRouteQuery(routeQuery)

    expect(parsed.object).toBe('NGC7000')
    expect(parsed.nearLat).toBeCloseTo(40.4)
    expect(parsed.nearLon).toBeCloseTo(-3.7)
    expect(parsed.km).toBe(25)
    expect(parsed.minFocal).toBe(135)
    expect(parsed.filter).toBe('OIII')
    expect(parsed.license).toEqual(['CC-BY-SA-4.0'])
    expect(parsed.minQuality).toBe(0.5)
    expect(parsed.tracked).toBe(false)
    expect(parsed.sort).toBe('recent')
  })

  it('no mete limit ni cursor en la URL', () => {
    const routeQuery = toRouteQuery({ object: 'M42', cursor: 'abc', limit: 10 })
    expect(routeQuery.limit).toBeUndefined()
    expect(routeQuery.cursor).toBeUndefined()
  })

  it('cuenta los filtros activos', () => {
    expect(countActiveFilters({})).toBe(0)
    expect(countActiveFilters({ object: 'M31', filter: 'Ha' })).toBe(2)
  })

  it('descarta un sort desconocido', () => {
    expect(fromRouteQuery({ sort: 'popularity' }).sort).toBeNull()
  })
})
