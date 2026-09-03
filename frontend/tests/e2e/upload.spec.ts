import { backendAvailable, expect, test } from './_helpers'

test.describe('subida de una foto', () => {
  test.beforeEach(async () => {
    test.skip(!(await backendAvailable()), 'El backend no está levantado (make dev)')
  })

  test('el formulario trae CC-BY-NC-4.0 preseleccionada y explica el congelado', async ({
    page,
  }) => {
    await page.goto('/upload')

    const dropzone = page.getByTestId('upload-dropzone')
    await expect(dropzone).toBeVisible()

    await page.getByTestId('upload-input').setInputFiles({
      name: 'm31.jpg',
      mimeType: 'image/jpeg',
      buffer: Buffer.from('fake-jpeg-bytes'),
    })

    await page.getByRole('button', { name: /Metadata|metadata/i }).first().click()

    const picker = page.getByTestId('license-picker')
    await expect(picker).toBeVisible()
    await expect(page.getByTestId('license-option-CC-BY-NC-4.0')).toBeChecked()
    await expect(picker).toContainText(/se congela/i)
  })

  test('una licencia ND deshabilita el uso como frame en reconstrucciones', async ({ page }) => {
    await page.goto('/upload')

    await page.getByTestId('upload-input').setInputFiles({
      name: 'm42.jpg',
      mimeType: 'image/jpeg',
      buffer: Buffer.from('fake-jpeg-bytes'),
    })
    await page.getByRole('button', { name: /Metadata|metadata/i }).first().click()

    await page.getByTestId('license-option-CC-BY-ND-4.0').check()

    await expect(page.getByTestId('allow-derivatives-in-stacks')).toBeDisabled()
    await expect(page.getByTestId('nd-forced-notice')).toBeVisible()
  })

  test('sube el fichero y lo deja en procesamiento', async ({ page }) => {
    await page.goto('/upload')

    await page.getByTestId('upload-input').setInputFiles({
      name: 'ngc7000.jpg',
      mimeType: 'image/jpeg',
      buffer: Buffer.from('fake-jpeg-bytes'),
    })
    await page.getByTestId('start-all-uploads').click()

    await expect(page.getByTestId('upload-success')).toBeVisible({ timeout: 30_000 })
  })
})
