/**
 * Port of `webapp/db.py`: timestamps, money helpers, transactions, schema.
 *
 * Two rules carried over exactly:
 *
 * **Money is integer cents.** Never float, never `Number` arithmetic on
 * dollars. `toCents` does string math with half-even rounding because that is
 * what Python's `Decimal.quantize` does under the default context — a port
 * that used `Math.round` would drift on exactly the inputs that matter.
 *
 * **Every mutation runs inside `transaction()`.** That is also where the undo
 * log will be written (Phase 1), so an operation and its inverse commit
 * together or not at all. `BEGIN IMMEDIATE` mirrors the Python side: a second
 * writer fails fast instead of deadlocking politely.
 */

/** Minimal surface of a sqlite-wasm oo1 DB this layer needs. */
export interface Database {
  exec(sql: string | { sql: string; bind?: unknown[] }): unknown
  selectValue(sql: string, bind?: unknown[]): unknown
  selectObject(sql: string, bind?: unknown[]): Record<string, unknown> | undefined
  selectObjects(sql: string, bind?: unknown[]): Record<string, unknown>[]
}

export const SCHEMA_VERSION = 1

// The wall clock, injectable as one module-level switch so the parity harness
// can freeze time across both backends without threading a parameter through
// every ported call site.
let clock: () => Date = () => new Date()

export function setClock(fn?: () => Date): void {
  clock = fn ?? (() => new Date())
}

/**
 * UTC ISO-8601, second precision, `+00:00` suffix — byte-identical to
 * Python's `datetime.now(timezone.utc).isoformat(timespec="seconds")`.
 * JS `toISOString()` says `…Z` instead; timestamps are string-sorted in SQL
 * and diffed by the parity gate, so the format is load-bearing, not cosmetic.
 */
export function now(at: () => Date = clock): string {
  const d = at()
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
    `T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}+00:00`
  )
}

/**
 * Dollars (string or number) -> integer cents; `null`/empty stays `null`.
 * `null` means "no price recorded", which is not the same as zero.
 *
 * Rounds half-even at the second decimal, matching `Decimal.quantize` under
 * Python's default context. Pure string/integer math — no doubles anywhere.
 */
export function toCents(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null
  const text = String(value).trim()
  const match = /^(-?)(\d*)(?:\.(\d*))?$/.exec(text)
  if (!match || (!match[2] && !match[3])) {
    throw new Error(`not a money amount: ${text}`)
  }
  const negative = match[1] === '-'
  const whole = match[2] || '0'
  const frac = match[3] ?? ''
  const firstTwo = (frac + '00').slice(0, 2)
  let cents = Number(whole) * 100 + Number(firstTwo)
  // Digits past the second decimal decide rounding. With trailing zeros
  // stripped, lexicographic comparison against '5' is exact: '51' > '5' is
  // r > 0.5, '4999' < '5' is r < 0.5, and exactly '5' is the tie.
  const rest = frac.slice(2).replace(/0+$/, '')
  if (rest > '5') cents += 1
  else if (rest === '5' && cents % 2 === 1) cents += 1 // ties go to the even cent
  return negative ? -cents : cents
}

/** Integer cents -> `$1,234.56` (or the dash for `null`), as `format_cents`. */
export function formatCents(cents: number | null | undefined, dash = '—'): string {
  if (cents === null || cents === undefined) return dash
  const sign = cents < 0 ? '-' : ''
  const abs = Math.abs(cents)
  const whole = Math.floor(abs / 100)
  const rest = abs % 100
  return `${sign}$${whole.toLocaleString('en-US')}.${String(rest).padStart(2, '0')}`
}

/**
 * Run `fn` inside BEGIN IMMEDIATE / COMMIT, rolling back on any throw.
 * Synchronous on purpose: the oo1 API over SAHPool is sync inside the worker,
 * which is what lets the ported domain code stay straight-line like Python.
 */
export function transaction<T>(db: Database, fn: () => T): T {
  db.exec('BEGIN IMMEDIATE')
  try {
    const result = fn()
    db.exec('COMMIT')
    return result
  } catch (error) {
    db.exec('ROLLBACK')
    throw error
  }
}

/** Idempotent schema init: exec the (IF NOT EXISTS) schema, stamp the version. */
export function initSchema(db: Database, schemaSql: string): number {
  db.exec(schemaSql)
  const version = db.selectValue('SELECT version FROM schema_version') as
    | number
    | undefined
  if (version === undefined || version === null) {
    db.exec(`INSERT INTO schema_version (version) VALUES (${SCHEMA_VERSION})`)
    return SCHEMA_VERSION
  }
  return version
}
