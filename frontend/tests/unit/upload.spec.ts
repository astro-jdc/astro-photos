import { describe, expect, it } from 'vitest'
import {
  ACCEPTED_EXTENSIONS,
  buildPresignedForm,
  inferMimeType,
  MULTIPART_THRESHOLD_BYTES,
} from '~/lib/upload'
import { exifToDraft, parseOffsetMinutes } from '~/lib/exif'

describe('inferMimeType', () => {
  it('respeta el tipo que da el navegador', () => {
    const file = new File(['x'], 'a.jpg', { type: 'image/jpeg' })
    expect(inferMimeType(file)).toBe('image/jpeg')
  })

  it('deduce el tipo de los RAW y los FITS, que el navegador deja vacíos', () => {
    expect(inferMimeType(new File(['x'], 'IMG_0001.CR3'))).toBe('image/x-canon-cr3')
    expect(inferMimeType(new File(['x'], 'm31.fits'))).toBe('image/fits')
    expect(inferMimeType(new File(['x'], 'raro.xyz'))).toBe('application/octet-stream')
  })

  it('acepta las extensiones esperadas', () => {
    expect(ACCEPTED_EXTENSIONS).toContain('.fits')
    expect(ACCEPTED_EXTENSIONS).toContain('.cr3')
    expect(ACCEPTED_EXTENSIONS).toContain('.tiff')
  })

  it('el umbral de multipart es 100 MB, como dice docs/api.md', () => {
    expect(MULTIPART_THRESHOLD_BYTES).toBe(100 * 1024 * 1024)
  })
})

describe('buildPresignedForm', () => {
  it('mete el fichero el último, como exige el POST presignado de S3', () => {
    const file = new File(['x'], 'a.jpg', { type: 'image/jpeg' })
    const form = buildPresignedForm({ key: 'staging/u/1', policy: 'p', signature: 's' }, file)
    const keys = [...form.keys()]
    expect(keys).toEqual(['key', 'policy', 'signature', 'file'])
  })
})

describe('exifToDraft', () => {
  it('marca vacío un fichero sin EXIF', () => {
    const draft = exifToDraft(null)
    expect(draft.empty).toBe(true)
    expect(draft.location).toBeNull()
    expect(draft.capturedAtLocal).toBeNull()
  })

  it('extrae hora local, GPS y óptica, y deriva la apertura', () => {
    const draft = exifToDraft({
      DateTimeOriginal: new Date(2026, 0, 15, 22, 5, 0),
      OffsetTimeOriginal: '+01:00',
      latitude: 28.3,
      longitude: -16.51,
      GPSAltitude: 2390,
      Make: 'Canon',
      Model: 'EOS R6',
      LensModel: 'RF 200mm',
      FocalLength: 200,
      FNumber: 2.8,
      ExposureTime: 30,
      ISO: 1600,
      ExifImageWidth: 6000,
      ExifImageHeight: 4000,
    })

    expect(draft.empty).toBe(false)
    expect(draft.capturedAtLocal).toBe('2026-01-15T22:05')
    expect(draft.utcOffsetMinutes).toBe(60)
    expect(draft.location).toEqual({
      lat: 28.3,
      lon: -16.51,
      elevation_m: 2390,
      accuracy_m: null,
    })
    expect(draft.equipment.camera_model).toBe('EOS R6')
    expect(draft.equipment.focal_length_mm).toBe(200)
    expect(draft.equipment.focal_ratio).toBeCloseTo(2.8, 2)
    // `aperture_mm` NO se manda: es un campo derivado que calcula el
    // backend (`focal_length_mm / focal_ratio`) y no existe en
    // `EquipmentIn`. El cliente manda lo medido, no lo derivado.
    expect('aperture_mm' in draft.equipment).toBe(false)
    expect(draft.widthPx).toBe(6000)
  })
})

describe('parseOffsetMinutes', () => {
  it('interpreta el desplazamiento EXIF', () => {
    expect(parseOffsetMinutes('+02:00')).toBe(120)
    expect(parseOffsetMinutes('-05:30')).toBe(-330)
    expect(parseOffsetMinutes('+0100')).toBe(60)
    expect(parseOffsetMinutes('mediodía')).toBeNull()
    expect(parseOffsetMinutes(undefined)).toBeNull()
  })
})
