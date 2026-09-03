import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import LicensePicker from '~/components/LicensePicker.vue'
import es from '../../i18n/locales/es.json'
import en from '../../i18n/locales/en.json'

function makeI18n() {
  return createI18n({
    legacy: false,
    locale: 'es',
    fallbackLocale: 'en',
    messages: { es, en },
  })
}

function factory(props: Record<string, unknown> = {}) {
  return mount(LicensePicker, {
    props,
    global: { plugins: [makeI18n()] },
  })
}

describe('LicensePicker', () => {
  it('preselecciona CC-BY-NC-4.0 y la marca como la opción por defecto', () => {
    const wrapper = factory()
    const input = wrapper.get('[data-testid="license-option-CC-BY-NC-4.0"]')
      .element as HTMLInputElement
    expect(input.checked).toBe(true)
    expect(wrapper.find('[data-testid="license-default-badge"]').exists()).toBe(true)
  })

  it('ofrece las 8 licencias del catálogo', () => {
    const wrapper = factory()
    expect(wrapper.findAll('input[type="radio"]')).toHaveLength(8)
  })

  it('emite el nuevo código al elegir otra licencia', async () => {
    const wrapper = factory()
    await wrapper.get('[data-testid="license-option-CC0-1.0"]').setValue()
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['CC0-1.0'])
  })

  it('un ND deshabilita el uso como frame en stacks y lo explica', async () => {
    const wrapper = factory({ modelValue: 'CC-BY-ND-4.0', allowDerivativesInStacks: true })
    await wrapper.vm.$nextTick()

    const checkbox = wrapper.get('[data-testid="allow-derivatives-in-stacks"]')
      .element as HTMLInputElement
    expect(checkbox.disabled).toBe(true)
    expect(checkbox.checked).toBe(false)
    expect(wrapper.find('[data-testid="nd-forced-notice"]').exists()).toBe(true)
    expect(wrapper.emitted('update:allowDerivativesInStacks')?.at(-1)).toEqual([false])
  })

  it('con una licencia no-ND la casilla de stacks queda utilizable', () => {
    const wrapper = factory({ modelValue: 'CC-BY-NC-4.0', allowDerivativesInStacks: true })
    const checkbox = wrapper.get('[data-testid="allow-derivatives-in-stacks"]')
      .element as HTMLInputElement
    expect(checkbox.disabled).toBe(false)
    expect(checkbox.checked).toBe(true)
    expect(wrapper.find('[data-testid="nd-forced-notice"]').exists()).toBe(false)
  })

  it('las dos casillas de consentimiento son independientes de la licencia', async () => {
    const wrapper = factory({ modelValue: 'CC0-1.0', allowAiTraining: true })
    const aiCheckbox = wrapper.get('[data-testid="allow-ai-training"]')
    await aiCheckbox.setValue(false)
    expect(wrapper.emitted('update:allowAiTraining')?.at(-1)).toEqual([false])
    // Cambiar el consentimiento de IA no toca la licencia.
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('avisa del congelado de licencia antes del primer cambio', () => {
    const wrapper = factory()
    expect(wrapper.text()).toContain('la licencia se congela')
  })

  it('con la licencia congelada solo deja relajarla', () => {
    const wrapper = factory({ modelValue: 'CC-BY-NC-4.0', lockedAt: '2026-01-01T00:00:00Z' })
    const moreRestrictive = wrapper.get('[data-testid="license-option-CC-BY-NC-SA-4.0"]')
      .element as HTMLInputElement
    const morePermissive = wrapper.get('[data-testid="license-option-CC-BY-4.0"]')
      .element as HTMLInputElement
    expect(moreRestrictive.disabled).toBe(true)
    expect(morePermissive.disabled).toBe(false)
  })

  it('no deja ninguna cadena literal fuera de i18n en la cabecera', () => {
    const wrapper = factory()
    expect(wrapper.text()).toContain('Licencia')
    expect(wrapper.text()).not.toContain('license.picker.')
  })
})
