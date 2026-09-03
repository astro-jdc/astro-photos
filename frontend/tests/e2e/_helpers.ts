import { test as base } from '@playwright/test'

const apiBase = process.env.E2E_API_BASE ?? 'http://localhost:8000/api/v1'

let backendUp: boolean | null = null

/**
 * Los E2E necesitan backend real (S3 presignado, cola, plate solving). Si no
 * está levantado se saltan en vez de fallar: `make dev` los habilita.
 */
export async function backendAvailable(): Promise<boolean> {
  if (backendUp !== null) return backendUp
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 2000)
    const response = await fetch(`${apiBase.replace(/\/api\/v1$/, '')}/readyz`, {
      signal: controller.signal,
    })
    clearTimeout(timer)
    backendUp = response.ok
  } catch {
    backendUp = false
  }
  return backendUp
}

export const test = base.extend({})
export { expect } from '@playwright/test'
export { apiBase }
