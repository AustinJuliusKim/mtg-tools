// Browser lane: real Chromium (the one Playwright already installed for e2e),
// loading bench_page.html over a local static server and timing parse.mjs
// in-page with performance.now().

import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { basename, dirname, extname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const BENCH = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(BENCH, '..')

// playwright-core comes from wherever the frontend deps are installed — this
// checkout, or (when running from a git worktree) the main checkout.
import { existsSync } from 'node:fs'
const PW_CANDIDATES = [
  process.env.BENCH_PLAYWRIGHT,
  join(REPO, 'frontend', 'node_modules', 'playwright-core', 'index.mjs'),
  resolve(REPO, '..', '..', '..', 'frontend', 'node_modules', 'playwright-core', 'index.mjs'),
].filter(Boolean)
const pwPath = PW_CANDIDATES.find((p) => existsSync(p))
if (!pwPath) throw new Error(`playwright-core not found; tried:\n${PW_CANDIDATES.join('\n')}`)
const { chromium } = await import(pwPath)

const args = process.argv.slice(2)
const lane = args[args.indexOf('--lane') + 1] ?? 'singles'
const runs = Number(args.includes('--runs') ? args[args.indexOf('--runs') + 1] : 7)
const files = args.filter((a) => !a.startsWith('--') && a.endsWith('.csv'))

const TYPES = { '.html': 'text/html', '.mjs': 'text/javascript', '.js': 'text/javascript', '.csv': 'text/csv' }

const server = createServer(async (req, res) => {
  try {
    const path = join(BENCH, decodeURIComponent(new URL(req.url, 'http://x').pathname))
    if (!path.startsWith(BENCH)) throw new Error('outside bench/')
    const body = await readFile(path)
    res.writeHead(200, { 'content-type': TYPES[extname(path)] ?? 'application/octet-stream' })
    res.end(body)
  } catch {
    res.writeHead(404).end()
  }
})
await new Promise((ok) => server.listen(0, '127.0.0.1', ok))
const port = server.address().port

const browser = await chromium.launch()
const page = await browser.newPage()
await page.goto(`http://127.0.0.1:${port}/js/bench_page.html`)
await page.waitForFunction('window.benchReady === true')

// Files are served relative to bench/ — the runner passes bench/data paths.
const urls = files.map((f) => `/${f.includes('data/') ? f.slice(f.indexOf('data/')) : basename(f)}`)
const result = await page.evaluate(
  ([urls, lane, runs]) => window.runBench(urls, lane, runs),
  [urls, lane, runs],
)

await browser.close()
server.close()

console.log(JSON.stringify({
  lane: `browser-${lane}`,
  chromium: browser.version?.() ?? 'chromium',
  files: files.map((f) => basename(f)),
  ...result,
}))
