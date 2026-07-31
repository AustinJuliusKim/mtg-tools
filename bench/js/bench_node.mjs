// Node lane: same parse.mjs the browser runs, timed with the same protocol
// as bench_python.py (2 warmups, N timed runs, per-stage medians).

import { readFileSync } from 'node:fs'
import { basename } from 'node:path'
import Papa from 'papaparse'

import { mergeRows, oracleOf, parseRows, parseSealed, sealedOracle, summarize } from './parse.mjs'

function median(xs) {
  const s = [...xs].sort((a, b) => a - b)
  const mid = Math.floor(s.length / 2)
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2
}

const args = process.argv.slice(2)
const lane = args[args.indexOf('--lane') + 1] ?? 'singles'
const runs = Number(args.includes('--runs') ? args[args.indexOf('--runs') + 1] : 7)
const warmup = 2
const files = args.filter((a) => !a.startsWith('--') && a.endsWith('.csv'))

const samples = []
let oracle = null

for (let i = 0; i < warmup + runs; i++) {
  const stages = {}
  let t = performance.now()
  const texts = files.map((f) => readFileSync(f, 'utf-8'))
  stages.read = performance.now() - t

  if (lane === 'sealed') {
    t = performance.now()
    const rows = texts.flatMap((text) => parseSealed(text, Papa))
    stages.parse = performance.now() - t
    oracle = { pre: sealedOracle(rows), post: sealedOracle(rows) }
  } else {
    t = performance.now()
    const rows = texts.flatMap((text) => parseRows(text, Papa))
    stages.parse = performance.now() - t
    t = performance.now()
    const merged = mergeRows(rows)
    stages.merge = performance.now() - t
    t = performance.now()
    summarize(merged)
    stages.summarize = performance.now() - t
    oracle = { pre: oracleOf(rows), post: oracleOf(merged) }
  }
  if (i >= warmup) samples.push(stages)
}

const medians = Object.fromEntries(
  Object.keys(samples[0]).map((k) => [k, Number(median(samples.map((s) => s[k])).toFixed(2))]),
)

console.log(JSON.stringify({
  lane: `node-${lane}`,
  node: process.version,
  files: files.map((f) => basename(f)),
  stagesMedianMs: medians,
  totalMedianMs: Number(Object.values(medians).reduce((a, b) => a + b, 0).toFixed(2)),
  runs,
  oracle,
  heapUsedMb: Number((process.memoryUsage().heapUsed / 1e6).toFixed(1)),
}))
