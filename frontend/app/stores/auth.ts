import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '~/lib/apiClient'
import { useApi, useAuthExpired, useAuthToken } from '~/composables/useApi'
import type { Me, MePatch } from '~/types/domain'

const STORAGE_KEY = 'astro-photos.session'

interface StoredSession {
  token: string
  refreshToken: string | null
  expiresAt: number | null
}

function readStored(): StoredSession | null {
  if (typeof localStorage === 'undefined') return null
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<StoredSession>
    if (typeof parsed.token !== 'string') return null
    return {
      token: parsed.token,
      refreshToken: typeof parsed.refreshToken === 'string' ? parsed.refreshToken : null,
      expiresAt: typeof parsed.expiresAt === 'number' ? parsed.expiresAt : null,
    }
  } catch {
    return null
  }
}

function writeStored(session: StoredSession | null) {
  if (typeof localStorage === 'undefined') return
  if (session) localStorage.setItem(STORAGE_KEY, JSON.stringify(session))
  else localStorage.removeItem(STORAGE_KEY)
}

export const useAuthStore = defineStore('auth', () => {
  const api = useApi()
  const token = useAuthToken()
  const expiredFlag = useAuthExpired()

  const refreshToken = ref<string | null>(null)
  const expiresAt = ref<number | null>(null)
  const profile = ref<Me | null>(null)
  const pending = ref(false)
  const error = ref<ApiError | null>(null)

  const isAuthenticated = computed(() => token.value !== null && profile.value !== null)
  const quota = computed(() => profile.value?.quota ?? null)
  const quotaRatio = computed(() => {
    const q = quota.value
    if (!q || q.quota_bytes <= 0) return 0
    return Math.min(1, q.used_bytes / q.quota_bytes)
  })
  const canQueueJob = computed(() => {
    const q = quota.value
    if (!q) return false
    return q.jobs_queued_now < q.max_queued_jobs && q.jobs_today < q.max_jobs_per_day
  })

  function setSession(next: StoredSession | null) {
    token.value = next?.token ?? null
    refreshToken.value = next?.refreshToken ?? null
    expiresAt.value = next?.expiresAt ?? null
    expiredFlag.value = false
    writeStored(next)
  }

  /** Rehidrata la sesión guardada y carga el perfil. Idempotente. */
  async function restore() {
    const stored = readStored()
    if (!stored) return
    setSession(stored)
    await fetchProfile()
  }

  async function fetchProfile() {
    if (!token.value) return
    pending.value = true
    error.value = null
    try {
      profile.value = await api.get<Me>('/me')
    } catch (e) {
      const apiError =
        e instanceof ApiError ? e : new ApiError({ status: 0, title: 'unknown_error' })
      error.value = apiError
      if (apiError.isAuthError) logout()
    } finally {
      pending.value = false
    }
  }

  async function updateProfile(patch: MePatch) {
    profile.value = await api.patch<Me>('/me', patch)
    return profile.value
  }

  /**
   * Refresco del JWT.
   *
   * STUB CONSCIENTE: el intercambio real del `refresh_token` contra el
   * dominio de Cognito depende de la configuración del User Pool, que aún no
   * está fijada (infra-dev). Mientras tanto: si el token caducó y no hay
   * refresh disponible, cerramos sesión de forma limpia en vez de dejar la UI
   * pidiendo cosas con un Bearer muerto.
   */
  async function refresh(): Promise<boolean> {
    if (!refreshToken.value) {
      logout()
      return false
    }
    // Cuando exista el endpoint, aquí va el POST al token endpoint de Cognito.
    logout()
    return false
  }

  function isExpiringSoon(marginSeconds = 120): boolean {
    if (expiresAt.value === null) return false
    return Date.now() >= expiresAt.value - marginSeconds * 1000
  }

  /** Llamar antes de una operación sensible; refresca si toca. */
  async function ensureFresh(): Promise<boolean> {
    if (!token.value) return false
    if (expiredFlag.value || isExpiringSoon()) return refresh()
    return true
  }

  function logout() {
    setSession(null)
    profile.value = null
  }

  return {
    token,
    refreshToken,
    expiresAt,
    profile,
    pending,
    error,
    isAuthenticated,
    quota,
    quotaRatio,
    canQueueJob,
    setSession,
    restore,
    fetchProfile,
    updateProfile,
    refresh,
    ensureFresh,
    isExpiringSoon,
    logout,
  }
})
