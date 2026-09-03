// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2025-07-15',

  future: { compatibilityVersion: 4 },

  devtools: { enabled: true },

  modules: [
    '@nuxtjs/tailwindcss',
    '@pinia/nuxt',
    '@nuxtjs/i18n',
    '@nuxt/image',
    '@vueuse/nuxt',
    '@nuxtjs/color-mode',
    '@nuxt/eslint',
  ],

  tailwindcss: {
    cssPath: ['~/assets/css/tailwind.css', { injectPosition: 'first' }],
    configPath: '~~/tailwind.config.ts',
    viewer: false,
  },

  typescript: {
    strict: true,
    typeCheck: false,
  },

  runtimeConfig: {
    public: {
      apiBase: process.env.NUXT_PUBLIC_API_BASE ?? 'http://localhost:8000/api/v1',
      siteUrl: process.env.NUXT_PUBLIC_SITE_URL ?? 'http://localhost:3000',
      mapStyleUrl:
        process.env.NUXT_PUBLIC_MAP_STYLE_URL ?? 'https://demotiles.maplibre.org/style.json',
    },
  },

  // Renderizado según el propósito (ver .claude/agents/frontend-dev.md, regla 2):
  // lo público y indexable se prerenderiza / ISR; lo personal es SPA.
  // `prefix_except_default` publica el inglés bajo /en, así que cada regla se
  // declara también para ese prefijo.
  routeRules: {
    '/': { prerender: true },
    '/en': { prerender: true },
    '/explore': { isr: 300 },
    '/en/explore': { isr: 300 },
    '/objects/**': { isr: 900 },
    '/en/objects/**': { isr: 900 },
    '/photos/**': { isr: 900 },
    '/en/photos/**': { isr: 900 },
    '/reconstructions/**': { isr: 300 },
    '/en/reconstructions/**': { isr: 300 },
    '/me/**': { ssr: false },
    '/en/me/**': { ssr: false },
    '/build': { ssr: false },
    '/build/**': { ssr: false },
    '/en/build': { ssr: false },
    '/en/build/**': { ssr: false },
    '/upload': { ssr: false },
    '/en/upload': { ssr: false },
  },

  colorMode: {
    classSuffix: '',
    preference: 'dark',
    fallback: 'dark',
    storageKey: 'astro-photos-color-mode',
  },

  i18n: {
    locales: [
      { code: 'es', language: 'es-ES', name: 'Español', file: 'es.json' },
      { code: 'en', language: 'en-GB', name: 'English', file: 'en.json' },
    ],
    defaultLocale: 'es',
    strategy: 'prefix_except_default',
    langDir: 'locales',
    detectBrowserLanguage: {
      useCookie: true,
      cookieKey: 'astro_photos_i18n',
      redirectOn: 'root',
      fallbackLocale: 'es',
    },
  },

  eslint: {
    config: { stylistic: false },
  },

  app: {
    head: {
      htmlAttrs: { lang: 'es' },
      meta: [
        { charset: 'utf-8' },
        { name: 'viewport', content: 'width=device-width, initial-scale=1, viewport-fit=cover' },
      ],
    },
  },

  vite: {
    optimizeDeps: { include: ['maplibre-gl', 'exifr'] },
  },
})
