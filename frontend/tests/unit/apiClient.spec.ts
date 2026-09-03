import { describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  buildQuery,
  createApiClient,
  joinUrl,
  toApiError,
} from '~/lib/apiClient'

function jsonResponse(body: unknown, init: ResponseInit & { contentType?: string } = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    statusText: init.statusText ?? '',
    headers: { 'content-type': init.contentType ?? 'application/json' },
  })
}

describe('buildQuery', () => {
  it('omite valores vacíos y serializa arrays con comas', () => {
    expect(
      buildQuery({
        object: 'M31',
        radius: 2,
        filter: '',
        cursor: null,
        limit: undefined,
        license: ['CC-BY-4.0', 'CC0-1.0'],
        tracked: true,
      }),
    ).toBe('?object=M31&radius=2&license=CC-BY-4.0%2CCC0-1.0&tracked=true')
  })

  it('devuelve cadena vacía sin parámetros', () => {
    expect(buildQuery(undefined)).toBe('')
    expect(buildQuery({})).toBe('')
  })
})

describe('joinUrl', () => {
  it('une base y ruta sin duplicar barras', () => {
    expect(joinUrl('http://api.test/api/v1/', '/photos')).toBe('http://api.test/api/v1/photos')
    expect(joinUrl('http://api.test/api/v1', 'photos')).toBe('http://api.test/api/v1/photos')
  })

  it('respeta una URL absoluta', () => {
    expect(joinUrl('http://api.test/api/v1', 'https://cdn.test/x.jpg')).toBe('https://cdn.test/x.jpg')
  })
})

describe('toApiError', () => {
  it('convierte un problem+json de RFC 9457 en ApiError', async () => {
    const response = jsonResponse(
      {
        type: 'https://astro-photos.dev/problems/license-blocked',
        title: 'license_blocked',
        status: 422,
        detail: 'Una de las fotos no permite derivadas',
        instance: '/api/v1/reconstructions',
        errors: [{ field: 'body/photo_ids', message: 'la foto 42 es ND', code: 'nd' }],
      },
      { status: 422, contentType: 'application/problem+json' },
    )

    const error = await toApiError(response)

    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(422)
    expect(error.title).toBe('license_blocked')
    expect(error.detail).toBe('Una de las fotos no permite derivadas')
    expect(error.type).toBe('https://astro-photos.dev/problems/license-blocked')
    expect(error.instance).toBe('/api/v1/reconstructions')
    expect(error.errors).toHaveLength(1)
    expect(error.fieldErrors).toEqual({ photo_ids: 'la foto 42 es ND' })
    expect(error.message).toBe('Una de las fotos no permite derivadas')
  })

  it('cae a un ApiError genérico si el cuerpo no es problem+json', async () => {
    const response = new Response('boom', {
      status: 500,
      statusText: 'Internal Server Error',
      headers: { 'content-type': 'text/plain' },
    })

    const error = await toApiError(response)

    expect(error.status).toBe(500)
    expect(error.title).toBe('Internal Server Error')
    expect(error.detail).toBe('boom')
    expect(error.problem).toBeNull()
  })

  it('marca 401 y 403 como errores de autenticación', async () => {
    const error = await toApiError(
      jsonResponse({ title: 'unauthorized', status: 401 }, { status: 401 }),
    )
    expect(error.isAuthError).toBe(true)
    expect(error.isNotFound).toBe(false)
  })
})

describe('createApiClient', () => {
  it('inyecta el Bearer y la baseURL', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ items: [], next_cursor: null }))
    const client = createApiClient({
      baseUrl: 'http://api.test/api/v1',
      getToken: () => 'jwt-123',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    })

    await client.get('/photos', { object: 'M31' })

    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://api.test/api/v1/photos?object=M31')
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer jwt-123')
  })

  it('no manda el Bearer en peticiones anónimas', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({}))
    const client = createApiClient({
      baseUrl: 'http://api.test/api/v1',
      getToken: () => 'jwt-123',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    })

    await client.get('/photos', undefined, { anonymous: true })

    const [, init] = fetchImpl.mock.calls[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined()
  })

  it('lanza ApiError y avisa del 401', async () => {
    const onUnauthorized = vi.fn()
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ title: 'expired_token', status: 401 }, { status: 401 }),
    )
    const client = createApiClient({
      baseUrl: 'http://api.test/api/v1',
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onUnauthorized,
    })

    await expect(client.get('/me')).rejects.toBeInstanceOf(ApiError)
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('convierte un fallo de red en ApiError con status 0', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError('failed to fetch')
    })
    const client = createApiClient({
      baseUrl: 'http://api.test/api/v1',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    })

    await expect(client.get('/photos')).rejects.toMatchObject({
      status: 0,
      title: 'network_error',
    })
  })

  it('serializa el cuerpo JSON en POST', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ ok: true }))
    const client = createApiClient({
      baseUrl: 'http://api.test/api/v1',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    })

    await client.post('/licenses/resolve', { photo_ids: ['a', 'b'] })

    const [, init] = fetchImpl.mock.calls[0] as [string, RequestInit]
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(init.body).toBe('{"photo_ids":["a","b"]}')
  })

  it('devuelve undefined en un 204', async () => {
    const fetchImpl = vi.fn(async () => new Response(null, { status: 204 }))
    const client = createApiClient({
      baseUrl: 'http://api.test/api/v1',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    })

    await expect(client.del('/photos/1')).resolves.toBeUndefined()
  })
})
