import { backendAvailable, expect, test } from './_helpers'

test.describe('constructor de reconstrucciones', () => {
  test.beforeEach(async () => {
    test.skip(!(await backendAvailable()), 'El backend no está levantado (make dev)')
  })

  test('el botón de lanzar está bloqueado hasta que se comprueba el plan', async ({ page }) => {
    await page.goto('/explore')

    const addButtons = page.getByRole('button', { name: /Añadir al constructor/i })
    await addButtons.nth(0).click()
    await addButtons.nth(1).click()

    await page.goto('/build')

    const launch = page.getByTestId('launch-reconstruction')
    await expect(launch).toBeDisabled()

    await page.getByTestId('run-preview').click()

    await expect(page.getByTestId('preview-selected')).toBeVisible({ timeout: 20_000 })
    await expect(page.getByTestId('preview-blocked')).toBeVisible()
  })

  test('el plan enseña la licencia resultante y el coste antes de encolar', async ({ page }) => {
    await page.goto('/explore')
    const addButtons = page.getByRole('button', { name: /Añadir al constructor/i })
    await addButtons.nth(0).click()
    await addButtons.nth(1).click()

    await page.goto('/build')
    await page.getByTestId('run-preview').click()

    const plan = page.getByText(/Licencia resultante/i)
    await expect(plan).toBeVisible({ timeout: 20_000 })
    await expect(page.getByText(/Coste estimado/i)).toBeVisible()
    await expect(page.getByText(/Cómputo estimado/i)).toBeVisible()
  })

  test('cambiar la selección invalida el plan comprobado', async ({ page }) => {
    await page.goto('/explore')
    const addButtons = page.getByRole('button', { name: /Añadir al constructor/i })
    await addButtons.nth(0).click()
    await addButtons.nth(1).click()

    await page.goto('/build')
    await page.getByTestId('run-preview').click()
    await expect(page.getByTestId('preview-selected')).toBeVisible({ timeout: 20_000 })

    await page.getByRole('button', { name: /Quitar/i }).first().click()

    await expect(page.getByTestId('launch-reconstruction')).toBeDisabled()
  })
})
