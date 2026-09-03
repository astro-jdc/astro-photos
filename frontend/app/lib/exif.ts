/**
 * Lectura de EXIF **en el navegador**, antes de subir nada: así el formulario
 * llega prerrellenado y el usuario corrige lo que haga falta. Lo que el usuario
 * cambie gana al EXIF y el backend lo marca con `*_source='user'`
 * (docs/api.md, paso 3 de la subida).
 */
import exifr from 'exifr'
import type { Equipment, GeoPointIn } from '~/types/domain'

export interface ExifDraft {
  capturedAtLocal: string | null
  utcOffsetMinutes: number | null
  location: GeoPointIn | null
  equipment: Equipment
  widthPx: number | null
  heightPx: number | null
  /** true si el fichero no traía EXIF utilizable. */
  empty: boolean
}

type RawExif = Record<string, unknown>

function num(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string') {
    const n = Number(value)
    if (Number.isFinite(n)) return n
  }
  return null
}

function str(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  return trimmed.length > 0 ? trimmed : null
}

function toLocalIso(value: unknown): string | null {
  if (value instanceof Date && !Number.isNaN(value.getTime())) {
    // Hora de pared del observador: sin sufijo Z, sin conversión.
    const p = (n: number) => String(n).padStart(2, '0')
    return `${value.getFullYear()}-${p(value.getMonth() + 1)}-${p(value.getDate())}T${p(
      value.getHours(),
    )}:${p(value.getMinutes())}`
  }
  return null
}

/** "+02:00" → 120 */
export function parseOffsetMinutes(offset: unknown): number | null {
  const s = str(offset)
  if (!s) return null
  const m = /^([+-])(\d{2}):?(\d{2})$/.exec(s)
  if (!m) return null
  const sign = m[1] === '-' ? -1 : 1
  return sign * (Number(m[2]) * 60 + Number(m[3]))
}

/** Convierte el volcado crudo de exifr al borrador que consume el formulario. */
export function exifToDraft(raw: RawExif | null | undefined): ExifDraft {
  const empty = !raw || Object.keys(raw).length === 0
  const data: RawExif = raw ?? {}

  const focal = num(data.FocalLength)
  const fNumber = num(data.FNumber)

  const lat = num(data.latitude)
  const lon = num(data.longitude)

  const equipment: Equipment = {
    camera_make: str(data.Make),
    camera_model: str(data.Model),
    lens_model: str(data.LensModel) ?? str(data.LensMake),
    focal_length_mm: focal,
    focal_ratio: fNumber,
    exposure_seconds: num(data.ExposureTime),
    iso: num(data.ISO),
  }

  return {
    capturedAtLocal:
      toLocalIso(data.DateTimeOriginal) ??
      toLocalIso(data.CreateDate) ??
      toLocalIso(data.ModifyDate),
    utcOffsetMinutes:
      parseOffsetMinutes(data.OffsetTimeOriginal) ?? parseOffsetMinutes(data.OffsetTime),
    location:
      lat !== null && lon !== null
        ? {
            lat,
            lon,
            elevation_m: num(data.GPSAltitude),
            accuracy_m: num(data.GPSHPositioningError),
          }
        : null,
    equipment,
    widthPx: num(data.ExifImageWidth) ?? num(data.ImageWidth),
    heightPx: num(data.ExifImageHeight) ?? num(data.ImageHeight),
    empty,
  }
}

/** Lee el EXIF de un fichero. Nunca lanza: un fichero sin EXIF es normal. */
export async function readExifDraft(file: File): Promise<ExifDraft> {
  try {
    const raw = (await exifr.parse(file, {
      tiff: true,
      exif: true,
      gps: true,
      xmp: true,
      translateValues: true,
      reviveValues: true,
    })) as RawExif | undefined
    return exifToDraft(raw)
  } catch {
    return exifToDraft(null)
  }
}
