/**
 * The route handlers — port of the `webapp/api.py` handler bodies, one named
 * function per route, returning exactly the JSON shapes Flask returns
 * (camelCase, integer cents, preformatted display strings).
 *
 * The worker owns the database and dispatches RPC to these. Handlers stay
 * synchronous (oo1 over SAHPool is sync); only the RPC edge is async.
 */

import { formatCents, transaction, type Database } from './db'
import { ApiFailure } from './errors'
import { UndoLookupError, latestUndoable, recent, undoOperation } from './operations'
import * as repo from './repo'

//: Query parameters that are not filters. Anything else must be a known filter.
const NON_FILTER_PARAMS = new Set(['sort', 'dir', 'page', 'perPage'])

interface Query {
  filters: repo.FilterValues
  sort?: string
  direction: string
  page: number
  perPage: number
}

/**
 * Merge and split the client's `{filters, opts}` exactly the way Flask splits
 * a query string: values are stringified (URLSearchParams would have),
 * undefined/''/false are dropped (the http transport's `query()` drops them
 * before they reach the wire), and any unknown key is a 400 — a typo'd filter
 * silently returning the whole collection is how a bulk edit goes wrong.
 */
function parseQuery(
  payload: { filters?: repo.FilterValues; opts?: repo.FilterValues } | undefined,
  spec: Record<string, unknown>,
): Query {
  const merged: Record<string, string> = {}
  for (const source of [payload?.filters, payload?.opts]) {
    for (const [key, value] of Object.entries(source ?? {})) {
      if (value === undefined || value === '' || value === false) continue
      merged[key] = String(value)
    }
  }

  const unknown = Object.keys(merged).filter(
    (key) => !(key in spec) && !NON_FILTER_PARAMS.has(key),
  )
  if (unknown.length) {
    const known = Object.keys(spec).sort().join(', ')
    throw new ApiFailure(
      `Unknown filter(s): ${unknown.sort().join(', ')}. Available: ${known}`,
      'bad-filter',
      400,
    )
  }

  const filters: repo.FilterValues = {}
  for (const key of Object.keys(spec)) {
    if (merged[key] !== undefined) filters[key] = merged[key]
  }
  return {
    filters,
    sort: merged.sort,
    direction: merged.dir ?? 'desc',
    page: parseInt(merged.page ?? '1', 10) || 1,
    perPage: parseInt(merged.perPage ?? '50', 10) || 50,
  }
}

function badFilter<T>(fn: () => T): T {
  try {
    return fn()
  } catch (error) {
    if (error instanceof ApiFailure) throw error
    throw new ApiFailure((error as Error).message, 'bad-filter', 400)
  }
}

// --- serializers (api.py's _holding/_sealed_row/_totals, verbatim shapes) ----

function holdingJson(row: Record<string, unknown>) {
  const price = row.price_cents as number | null
  const quantity = row.quantity as number
  const total = price !== null ? price * quantity : null
  return {
    id: row.id,
    title: row.title,
    edition: row.edition,
    setName: row.set_name,
    collectorNumber: row.collector_number,
    rarity: row.rarity,
    foil: Boolean(row.foil),
    quantity,
    priceCents: price,
    totalCents: total,
    price: formatCents(price),
    total: formatCents(total),
    condition: row.condition,
    language: row.language,
    verdict: row.verdict,
  }
}

function sealedJson(row: Record<string, unknown>) {
  const price = row.price_cents as number | null
  const quantity = row.quantity as number
  const total = price !== null ? price * quantity : null
  const basis = row.cost_basis_cents as number | null
  const cost = basis !== null ? basis * quantity : null
  // Gain stays null without a basis — never zero. A fabricated basis lands
  // straight in a tax figure.
  const gain = cost === null || total === null ? null : total - cost
  return {
    id: row.id,
    name: (row.product_name as string) || row.raw_name,
    rawName: row.raw_name,
    setCode: row.set_code,
    setName: row.set_name,
    year: row.release_year,
    quantity,
    priceCents: price,
    totalCents: total,
    price: formatCents(price),
    total: formatCents(total),
    costBasisCents: cost,
    costBasis: formatCents(cost),
    gainCents: gain,
    gain: formatCents(gain),
    priceDate: row.price_date,
    priceSource: row.price_source,
    condition: row.condition,
    resolved: Boolean(row.resolved),
    purchaseUrl: row.purchase_url,
    notes: row.notes,
    verdict: row.verdict,
  }
}

function totalsJson(t: repo.Totals) {
  return {
    rows: t.rows,
    quantity: t.quantity,
    valueCents: t.value_cents,
    value: formatCents(t.value_cents),
    unpriced: t.unpriced,
  }
}

function sealedTotalsJson(t: repo.SealedTotals) {
  return {
    ...totalsJson(t),
    unresolved: t.unresolved,
    costCents: t.cost_cents,
    cost: formatCents(t.cost_cents),
  }
}

//: bulk.py's ACTIONS metadata (the run functions arrive in phase 4).
const BULK_ACTIONS: Array<{
  key: string
  label: string
  needsValue: boolean
  destructive: boolean
  kinds: string[]
}> = [
  { key: 'verdict', label: 'Set verdict', needsValue: true, destructive: false, kinds: ['holding', 'sealed'] },
  { key: 'condition', label: 'Set condition', needsValue: true, destructive: false, kinds: ['holding', 'sealed'] },
  { key: 'language', label: 'Set language', needsValue: true, destructive: false, kinds: ['holding'] },
  { key: 'price', label: 'Set price', needsValue: true, destructive: false, kinds: ['holding', 'sealed'] },
  { key: 'adjust_price', label: 'Adjust price by %', needsValue: true, destructive: false, kinds: ['holding', 'sealed'] },
  { key: 'cost_basis', label: 'Set cost basis', needsValue: true, destructive: false, kinds: ['sealed'] },
  { key: 'delete', label: 'Delete', needsValue: false, destructive: true, kinds: ['holding', 'sealed'] },
]

// --- the routes --------------------------------------------------------------

export interface WorkerContext {
  db(): Database
  vfs(): string
  schemaVersion(): number
  importDatabase(bytes: Uint8Array): Promise<{ holdings: number; sealed: number }>
}

type QueryPayload = { filters?: repo.FilterValues; opts?: repo.FilterValues }

export function makeRoutes(ctx: WorkerContext) {
  return {
    ping: () => ({ status: 'ok', vfs: ctx.vfs(), schemaVersion: ctx.schemaVersion() }),

    session: () => ({
      csrfToken: '', // no server, no cookies, nothing for CSRF to defend
      database:
        ctx.vfs() === 'opfs-sahpool' ? 'opfs:/collection.db' : ':memory: (nothing persists)',
      undoable: latestUndoable(ctx.db()),
    }),

    history: () => recent(ctx.db(), 50),

    undo: () => {
      try {
        return transaction(ctx.db(), () => undoOperation(ctx.db()))
      } catch (error) {
        if (error instanceof UndoLookupError) {
          throw new ApiFailure(error.message, 'nothing-to-undo', 409)
        }
        throw error
      }
    },

    collection: (payload: QueryPayload) => {
      const q = parseQuery(payload, repo.FILTERS)
      const db = ctx.db()
      const page = badFilter(() =>
        repo.queryHoldings(db, q.filters, {
          sort: q.sort ?? repo.DEFAULT_SORT,
          direction: q.direction,
          page: q.page,
          perPage: q.perPage,
        }),
      )
      return {
        rows: page.rows.map(holdingJson),
        page: page.page,
        perPage: page.perPage,
        pages: page.pages,
        totalRows: page.totalRows,
        sort: page.sort,
        direction: page.direction,
        totals: totalsJson(badFilter(() => repo.totals(db, q.filters))),
        grandTotals: totalsJson(repo.totals(db, {})),
        facets: {
          editions: repo.distinctValues(db, 'edition'),
          rarities: repo.distinctValues(db, 'rarity'),
          conditions: repo.distinctValues(db, 'condition'),
        },
      }
    },

    insights: (payload: QueryPayload) => {
      const q = parseQuery(payload, repo.FILTERS)
      const db = ctx.db()
      return badFilter(() => ({
        concentration: repo.concentration(db, q.filters),
        tiers: repo.tierBreakdown(db, q.filters).map((t) => ({
          ...t,
          market: formatCents(t.marketCents),
          cash: formatCents(t.cashCents),
          credit: formatCents(t.creditCents),
        })),
        sets: repo.topSets(db, q.filters).map((s) => ({ ...s, value: formatCents(s.cents) })),
        rarity: repo.raritySplit(db, q.filters).map((r) => ({ ...r, value: formatCents(r.cents) })),
        totals: totalsJson(repo.totals(db, q.filters)),
      }))
    },

    sealed: (payload: QueryPayload) => {
      const q = parseQuery(payload, repo.SEALED_FILTERS)
      const db = ctx.db()
      const page = badFilter(() =>
        repo.querySealed(db, q.filters, {
          sort: q.sort ?? 'total',
          direction: q.direction,
          page: q.page,
          perPage: q.perPage,
        }),
      )
      return {
        rows: page.rows.map(sealedJson),
        page: page.page,
        perPage: page.perPage,
        pages: page.pages,
        totalRows: page.totalRows,
        sort: page.sort,
        direction: page.direction,
        totals: sealedTotalsJson(badFilter(() => repo.sealedTotals(db, q.filters))),
        grandTotals: sealedTotalsJson(repo.sealedTotals(db, {})),
        facets: {
          sets: repo.sealedDistinct(db, 'set_code'),
          years: repo.sealedDistinct(db, 'release_year'),
          conditions: repo.sealedDistinct(db, 'condition'),
        },
      }
    },

    sealedInsights: (payload: QueryPayload) => {
      const q = parseQuery(payload, repo.SEALED_FILTERS)
      const db = ctx.db()
      return badFilter(() => {
        const totals = repo.sealedTotals(db, q.filters)
        return {
          byYear: repo.sealedByYear(db, q.filters).map((y) => ({
            year: y.year,
            quantity: y.qty,
            cents: y.cents,
            value: formatCents(y.cents as number),
            unpriced: y.unpriced,
          })),
          coverage: {
            priced: totals.quantity - totals.unpriced,
            unpriced: totals.unpriced,
            pricedCents: totals.value_cents,
          },
          totals: sealedTotalsJson(totals),
        }
      })
    },

    bulkActions: (payload: { kind?: string }) => {
      const kind = payload?.kind ?? 'holding'
      if (!(kind in repo.SUBJECTS)) {
        throw new ApiFailure(`'${kind}' is not a bulk subject`, 'bad-kind', 400)
      }
      return BULK_ACTIONS.filter((a) => a.kinds.includes(kind)).map(
        ({ key, label, needsValue, destructive }) => ({ key, label, needsValue, destructive }),
      )
    },

    importDatabase: async (payload: { file: File }) => {
      const bytes = new Uint8Array(await payload.file.arrayBuffer())
      const counts = await ctx.importDatabase(bytes)
      return { imported: true, ...counts }
    },
  }
}
