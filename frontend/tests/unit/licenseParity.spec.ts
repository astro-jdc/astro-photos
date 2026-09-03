/**
 * La pista local de licencias no puede divergir del backend.
 *
 * `app/lib/licensing.ts` reimplementa la combinación de licencias para poder
 * pintar la interfaz sin esperar al servidor. Eso roza la regla dura 5 de
 * CLAUDE.md («la lógica vive en un único sitio»), y se tolera **solo** porque
 * es una pista optimista que nunca decide nada: el constructor sigue obligado
 * a llamar a `POST /reconstructions/preview`.
 *
 * Lo que no se puede tolerar es que las dos versiones se separen sin que nadie
 * se entere: una pista que dice «CC-BY» donde el servidor dirá «CC-BY-NC» hace
 * que el usuario elija fotos con una expectativa legal falsa.
 *
 * La tabla de `tests/fixtures/license-table.json` la **genera el backend** con
 * `resolve_output_license()` (ver `tests/contract/test_license_parity.py`).
 * Este test la recorre entera: las 8 licencias, en combinaciones de 1, 2 y 3.
 */
import { describe, expect, it } from 'vitest'
import table from '../fixtures/license-table.json'
import { resolveOutputLicenseHint } from '~/lib/licensing'
import type { LicenseCode } from '~/types/domain'

interface Case {
  inputs: string[]
  resulting_license: string | null
  blocked: string[]
}

const cases = table.cases as Case[]

describe('paridad con backend/app/domain/licensing.py', () => {
  it('la tabla generada no está vacía', () => {
    // Sin esto, un fixture vacío haría pasar el test de abajo sin comprobar nada.
    expect(cases.length).toBeGreaterThan(100)
  })

  it.each(cases.map((c) => [c.inputs.join(' + '), c] as const))(
    'coincide para %s',
    (_label, testCase) => {
      const hint = resolveOutputLicenseHint(
        testCase.inputs.map((code) => ({
          license: code as LicenseCode,
          allowDerivativesInStacks: true,
        })),
      )

      if (testCase.blocked.length > 0) {
        // El backend bloquea: la pista no puede ofrecer una licencia de salida.
        expect(hint.license).toBeNull()
        expect(hint.blocked.length).toBeGreaterThan(0)
      } else {
        expect(hint.blocked).toEqual([])
        expect(hint.license).toBe(testCase.resulting_license)
      }
    },
  )
})
