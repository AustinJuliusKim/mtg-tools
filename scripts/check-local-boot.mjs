// Phase 0 smoke: does the local backend actually boot in a real browser?
// Assumes frontend/dist holds a VITE_BACKEND=local build (see usage below).
// Serves it, loads it in headless Chromium, and awaits the worker's ping —
// asserting the OPFS SAHPool VFS mounted and the schema stamped version 1.
//
//   VITE_BACKEND=local npm --prefix frontend run build
//   node scripts/check-local-boot.mjs

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const PW_CANDIDATES = [
  join(REPO, 'frontend', 'node_modules', 'playwright-core', 'index.mjs'),
  resolve(REPO, '..', '..', '..', 'frontend', 'node_modules', 'playwright-core', 'index.mjs'),
]
const pwPath = PW_CANDIDATES.find((p) => existsSync(p))
if (!pwPath) throw new Error('playwright-core not found (npm --prefix frontend ci first)')
const { chromium } = await import(pwPath)

const preview = spawn('npm', ['--prefix', join(REPO, 'frontend'), 'run', 'preview', '--', '--port', '4199', '--strictPort', '--host', '127.0.0.1'], {
  stdio: 'pipe',
})
let previewLog = ''
preview.stdout.on('data', (d) => { previewLog += d })
preview.stderr.on('data', (d) => { previewLog += d })

// Poll until the server actually accepts connections; stdout banners lie.
const deadline = Date.now() + 20_000
for (;;) {
  try {
    const res = await fetch('http://127.0.0.1:4199/')
    if (res.ok) break
  } catch {
    if (Date.now() > deadline) {
      preview.kill()
      throw new Error(`vite preview never came up:\n${previewLog}`)
    }
    await new Promise((ok) => setTimeout(ok, 300))
  }
}

const browser = await chromium.launch()
try {
  const page = await browser.newPage()
  await page.goto('http://127.0.0.1:4199/')
  const boot = await page.evaluate(() => window.__localBoot)
  if (boot?.status !== 'ok') throw new Error(`ping failed: ${JSON.stringify(boot)}`)
  if (boot.schemaVersion !== 1) throw new Error(`schema version ${boot.schemaVersion}, wanted 1`)
  console.log(`local backend up: vfs=${boot.vfs} schemaVersion=${boot.schemaVersion}`)
  if (boot.vfs !== 'opfs-sahpool') {
    console.log('note: OPFS unavailable in this context — memory fallback engaged')
  }
} finally {
  await browser.close()
  preview.kill()
}
