import type { Config } from 'tailwindcss'

export default {
  darkMode: 'class',
  content: [
    './app/components/**/*.{vue,ts}',
    './app/layouts/**/*.vue',
    './app/pages/**/*.vue',
    './app/plugins/**/*.ts',
    './app/app.vue',
    './app/error.vue',
  ],
  theme: {
    extend: {
      colors: {
        // Paleta pensada para uso nocturno: fondos muy oscuros, acentos fríos.
        night: {
          50: '#eef2ff',
          100: '#dbe3ff',
          200: '#b9c8ff',
          300: '#8ea4f5',
          400: '#6b83dd',
          500: '#4d63b8',
          600: '#3a4c92',
          700: '#2b386c',
          800: '#1b2445',
          900: '#0f1428',
          950: '#070a16',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'monospace'],
      },
    },
  },
  plugins: [],
} satisfies Config
