/**
 * Catálogo de licencias del lado del cliente.
 *
 * OJO: esto es **solo para la interfaz** (orden, badges, avisos, pistas
 * optimistas). La verdad vive en `backend/app/domain/licensing.py` y se
 * consulta con `POST /licenses/resolve`; el constructor de reconstrucciones
 * nunca decide por su cuenta (docs/licensing.md).
 */
import type { LicenseCode } from '~/types/domain'

export interface LicenseFacts {
  code: LicenseCode
  /** Clave de i18n para el nombre legible. */
  nameKey: string
  /** Clave de i18n para la explicación en lenguaje llano. */
  descriptionKey: string
  url: string | null
  spdxId: string | null
  allowsCommercial: boolean
  allowsDerivatives: boolean
  requiresAttribution: boolean
  requiresShareAlike: boolean
  restrictiveness: number
}

/** Orden y flags de docs/licensing.md. */
export const LICENSES: readonly LicenseFacts[] = [
  {
    code: 'CC0-1.0',
    nameKey: 'license.CC0-1.0.name',
    descriptionKey: 'license.CC0-1.0.description',
    url: 'https://creativecommons.org/publicdomain/zero/1.0/',
    spdxId: 'CC0-1.0',
    allowsCommercial: true,
    allowsDerivatives: true,
    requiresAttribution: false,
    requiresShareAlike: false,
    restrictiveness: 0,
  },
  {
    code: 'CC-BY-4.0',
    nameKey: 'license.CC-BY-4.0.name',
    descriptionKey: 'license.CC-BY-4.0.description',
    url: 'https://creativecommons.org/licenses/by/4.0/',
    spdxId: 'CC-BY-4.0',
    allowsCommercial: true,
    allowsDerivatives: true,
    requiresAttribution: true,
    requiresShareAlike: false,
    restrictiveness: 1,
  },
  {
    code: 'CC-BY-SA-4.0',
    nameKey: 'license.CC-BY-SA-4.0.name',
    descriptionKey: 'license.CC-BY-SA-4.0.description',
    url: 'https://creativecommons.org/licenses/by-sa/4.0/',
    spdxId: 'CC-BY-SA-4.0',
    allowsCommercial: true,
    allowsDerivatives: true,
    requiresAttribution: true,
    requiresShareAlike: true,
    restrictiveness: 2,
  },
  {
    code: 'CC-BY-NC-4.0',
    nameKey: 'license.CC-BY-NC-4.0.name',
    descriptionKey: 'license.CC-BY-NC-4.0.description',
    url: 'https://creativecommons.org/licenses/by-nc/4.0/',
    spdxId: 'CC-BY-NC-4.0',
    allowsCommercial: false,
    allowsDerivatives: true,
    requiresAttribution: true,
    requiresShareAlike: false,
    restrictiveness: 3,
  },
  {
    code: 'CC-BY-NC-SA-4.0',
    nameKey: 'license.CC-BY-NC-SA-4.0.name',
    descriptionKey: 'license.CC-BY-NC-SA-4.0.description',
    url: 'https://creativecommons.org/licenses/by-nc-sa/4.0/',
    spdxId: 'CC-BY-NC-SA-4.0',
    allowsCommercial: false,
    allowsDerivatives: true,
    requiresAttribution: true,
    requiresShareAlike: true,
    restrictiveness: 4,
  },
  {
    code: 'CC-BY-ND-4.0',
    nameKey: 'license.CC-BY-ND-4.0.name',
    descriptionKey: 'license.CC-BY-ND-4.0.description',
    url: 'https://creativecommons.org/licenses/by-nd/4.0/',
    spdxId: 'CC-BY-ND-4.0',
    allowsCommercial: true,
    allowsDerivatives: false,
    requiresAttribution: true,
    requiresShareAlike: false,
    restrictiveness: 5,
  },
  {
    code: 'CC-BY-NC-ND-4.0',
    nameKey: 'license.CC-BY-NC-ND-4.0.name',
    descriptionKey: 'license.CC-BY-NC-ND-4.0.description',
    url: 'https://creativecommons.org/licenses/by-nc-nd/4.0/',
    spdxId: 'CC-BY-NC-ND-4.0',
    allowsCommercial: false,
    allowsDerivatives: false,
    requiresAttribution: true,
    requiresShareAlike: false,
    restrictiveness: 6,
  },
  {
    code: 'ARR',
    nameKey: 'license.ARR.name',
    descriptionKey: 'license.ARR.description',
    url: null,
    spdxId: null,
    allowsCommercial: false,
    allowsDerivatives: false,
    requiresAttribution: true,
    requiresShareAlike: false,
    restrictiveness: 7,
  },
] as const

/** La preselección pedida en docs/licensing.md. */
export const DEFAULT_LICENSE: LicenseCode = 'CC-BY-NC-4.0'

const BY_CODE = new Map<LicenseCode, LicenseFacts>(LICENSES.map((l) => [l.code, l]))

export function licenseFacts(code: LicenseCode): LicenseFacts {
  const facts = BY_CODE.get(code)
  if (!facts) throw new Error(`Licencia desconocida: ${code}`)
  return facts
}

/**
 * Un ND (o ARR) implica `allow_derivatives_in_stacks = false` de forma
 * forzosa: la foto no puede entrar como frame en la reconstrucción de nadie.
 * La UI lo explica en vez de dejar un estado incoherente.
 */
export function forbidsStackDerivatives(code: LicenseCode): boolean {
  return !licenseFacts(code).allowsDerivatives
}

/** Códigos que permiten uso comercial: atajo `usable_for=commercial`. */
export function commercialLicenseCodes(): LicenseCode[] {
  return LICENSES.filter((l) => l.allowsCommercial).map((l) => l.code)
}

/**
 * Pista optimista de la licencia resultante de combinar N entradas.
 *
 * Réplica del algoritmo de docs/licensing.md para poder pintar la UI sin
 * esperar al servidor. **No sustituye** a `POST /licenses/resolve`: el
 * constructor sigue obligado a llamar a `POST /reconstructions/preview` antes
 * de habilitar el botón de lanzar.
 */
export interface LocalResolution {
  license: LicenseCode | null
  blocked: LicenseCode[]
}

export function resolveOutputLicenseHint(
  inputs: { license: LicenseCode; allowDerivativesInStacks: boolean }[],
): LocalResolution {
  const blocked = inputs
    .filter((i) => !i.allowDerivativesInStacks || forbidsStackDerivatives(i.license))
    .map((i) => i.license)
  if (blocked.length > 0) return { license: null, blocked }

  const usable = inputs.filter(
    (i) => i.allowDerivativesInStacks && !forbidsStackDerivatives(i.license),
  )
  if (usable.length === 0) return { license: null, blocked: [] }

  const needsNc = usable.some((i) => !licenseFacts(i.license).allowsCommercial)
  const needsSa = usable.some((i) => licenseFacts(i.license).requiresShareAlike)

  const candidates = LICENSES.filter(
    (l) =>
      l.allowsDerivatives &&
      l.allowsCommercial === !needsNc &&
      l.requiresShareAlike === needsSa &&
      l.code !== 'ARR',
  )
  if (candidates.length === 0) return { license: null, blocked: [] }

  // Si no hay NC ni SA, hereda la más restrictiva presente (CC0 solo sale si
  // todas las entradas son CC0).
  if (!needsNc && !needsSa) {
    const maxPresent = Math.max(...usable.map((i) => licenseFacts(i.license).restrictiveness))
    const inherited = candidates.filter((l) => l.restrictiveness >= maxPresent)
    const chosen = inherited[0] ?? candidates[candidates.length - 1]
    return { license: chosen ? chosen.code : null, blocked: [] }
  }

  const chosen = candidates[0]
  return { license: chosen ? chosen.code : null, blocked: [] }
}
