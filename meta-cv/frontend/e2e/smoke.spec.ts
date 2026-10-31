import { expect, test } from '@playwright/test'

test('page loads, an example runs, the trace and answer appear, results render', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))

  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Hello' })).toBeVisible()
  await expect(page.getByText('Scripted demo (no model)')).toBeVisible()

  await page.getByRole('button', { name: /Reuse a verified calculation/ }).click()
  await expect(page.getByText(/Answer: 80/).first()).toBeVisible({ timeout: 45_000 })
  await page.getByRole('button', { name: /See the thinking/ }).first().click()
  await expect(page.locator('.trace-step').first()).toBeVisible()
  await expect(page.getByText('Was this right?').first()).toBeVisible()

  await page.getByRole('button', { name: 'Results' }).click()
  await expect(page).toHaveURL(/\/results$/)
  await expect(page.getByRole('heading', { name: 'Results', level: 1 })).toBeVisible()

  expect(errors).toEqual([])
})
