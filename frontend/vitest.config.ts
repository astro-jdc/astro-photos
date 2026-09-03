import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

const r = (p: string) => fileURLToPath(new URL(p, import.meta.url))

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      // Stubs de los alias de Nuxt para poder probar composables y componentes
      // sin arrancar un servidor Nuxt completo.
      '#app': r('./tests/stubs/nuxt-app.ts'),
      '#imports': r('./tests/stubs/nuxt-app.ts'),
      '~': r('./app'),
      '@': r('./app'),
    },
  },
  test: {
    environment: 'happy-dom',
    include: ['tests/unit/**/*.spec.ts'],
    globals: true,
    restoreMocks: true,
  },
})
