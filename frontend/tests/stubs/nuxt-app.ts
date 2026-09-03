/**
 * Stub de los alias `#app` / `#imports` para los tests unitarios.
 *
 * Solo cubre lo que los composables y stores importan explícitamente, de modo
 * que Vitest pueda cargarlos sin arrancar un servidor Nuxt entero.
 */
import { ref, type Ref } from 'vue'

const states = new Map<string, Ref<unknown>>()

export function useState<T>(key: string, init?: () => T): Ref<T> {
  const existing = states.get(key)
  if (existing) return existing as Ref<T>
  const created = ref(init ? init() : undefined) as Ref<T>
  states.set(key, created as Ref<unknown>)
  return created
}

export function clearNuxtState(): void {
  states.clear()
}

export interface TestRuntimeConfig {
  public: Record<string, unknown>
}

let runtimeConfig: TestRuntimeConfig = {
  public: {
    apiBase: 'http://api.test/api/v1',
    siteUrl: 'http://localhost:3000',
    mapStyleUrl: 'http://localhost/style.json',
  },
}

export function useRuntimeConfig(): TestRuntimeConfig {
  return runtimeConfig
}

export function setRuntimeConfig(next: TestRuntimeConfig): void {
  runtimeConfig = next
}

export function useAsyncData<T>(_key: string, handler: () => Promise<T>) {
  return { data: ref<T | null>(null), error: ref(null), pending: ref(false), refresh: handler }
}

export function useSeoMeta(_meta: Record<string, unknown>): void {}
export function useHead(_head: unknown): void {}
export function navigateTo(to: string): string {
  return to
}
export function clearError(_options?: { redirect?: string }): void {}
export function useColorMode() {
  return ref({ preference: 'dark', value: 'dark' })
}

export interface NuxtError {
  statusCode: number
  statusMessage?: string
  message?: string
}
