import { defineConfig } from '@playwright/test'

/**
 * Smoke test against the scripted demo backend (no model, no Aura) and the Vite dev server.
 *   npx playwright install chromium   # once, or set PW_CHANNEL=msedge / chrome to use an installed browser
 *   npx playwright test
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  use: {
    baseURL: 'http://localhost:5173',
    channel: process.env.PW_CHANNEL || undefined,
  },
  webServer: [
    {
      command: process.platform === 'win32' ? '..\\..\\venv\\Scripts\\python ..\\demo_server.py' : '../../venv/bin/python ../demo_server.py',
      url: 'http://localhost:8000/healthz',
      reuseExistingServer: true,
      timeout: 60_000,
    },
    { command: 'npm run dev -- --port 5173 --strictPort', url: 'http://localhost:5173', reuseExistingServer: true },
  ],
})
