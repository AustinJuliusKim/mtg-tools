/**
 * Typed client for the collection API.
 *
 * Two rules mirrored from the server, because breaking either here would undo
 * the guarantees it enforces:
 *
 * 1. **Money arrives as integer cents plus a preformatted string.** Nothing in
 *    the UI does money arithmetic — JSON numbers are doubles, and the whole
 *    stack keeps money exact precisely to avoid that.
 *
 * 2. **A bulk selection is never a materialized id list when it means
 *    "everything matching".** `selectAll` plus the filters goes to the server,
 *    which re-resolves it, so a filter that changed since render cannot widen
 *    an edit.
 */

export type Filters = Record<string, string | number | boolean | undefined>

export interface Holding {
  id: number
  title: string
  edition: string
  setName: string
  collectorNumber: string
  rarity: string
  foil: boolean
  quantity: number
  priceCents: number | null
  totalCents: number | null
  price: string
  total: string
  condition: string
  language: string
  verdict: 'keep' | 'sell' | 'undecided'
}

export interface Totals {
  rows: number
  quantity: number
  valueCents: number
  value: string
  unpriced: number
}

export interface CollectionPage {
  rows: Holding[]
  page: number
  perPage: number
  pages: number
  totalRows: number
  sort: string
  direction: 'asc' | 'desc'
  totals: Totals
  grandTotals: Totals
  facets: { editions: string[]; rarities: string[]; conditions: string[] }
}

export interface Tier {
  tier: string
  label: string
  quantity: number
  marketCents: number
  cashCents: number
  creditCents: number
  cashPct: number
  creditPct: number
  market: string
  cash: string
  credit: string
}

export interface Insights {
  concentration: {
    points: { n: number; rowPct: number; valuePct: number }[]
    marks: { valuePct: number; rows: number }[]
    pricedRows: number
  }
  tiers: Tier[]
  sets: { name: string; quantity: number; cents: number; value: string; other: boolean }[]
  rarity: { name: string; quantity: number; cents: number; value: string }[]
  totals: Totals
}

export interface Operation {
  id: number
  kind: string
  summary: string
  affected: number
  createdAt: string
  revertedAt: string | null
  reverted: boolean
}

export interface ImportRecord {
  id: number
  filename: string
  kind: string
  dialect: string
  rowCount: number
  status: string
  createdAt: string
  committedAt: string | null
}

export interface ImportDetail {
  record: ImportRecord
  blocking: number
  blockingCodes: string[]
  issues: {
    code: string
    blocking: boolean
    rows: { id: number; lineNo: number; name: string; candidates: string[]; state: string }[]
  }[]
}

export interface BulkAction {
  key: string
  label: string
  needsValue: boolean
  destructive: boolean
}

export interface Selection {
  ids?: number[]
  selectAll?: boolean
  filters?: Filters
}

export class ApiError extends Error {
  code: string
  status: number
  constructor(message: string, code: string, status: number) {
    super(message)
    this.code = code
    this.status = status
  }
}

let csrfToken = ''

function query(filters: Filters = {}, extra: Filters = {}): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries({ ...filters, ...extra })) {
    if (value === undefined || value === '' || value === false) continue
    params.set(key, String(value))
  }
  const text = params.toString()
  return text ? `?${text}` : ''
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  if (method !== 'GET' && method !== 'HEAD') {
    // A custom header is the part that actually blocks cross-origin form
    // posts; the token is the second factor.
    headers.set('X-CSRF-Token', csrfToken)
  }

  const response = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  const isJson = response.headers.get('content-type')?.includes('application/json')
  const body = isJson ? await response.json() : null

  if (!response.ok) {
    throw new ApiError(
      body?.error ?? `Request failed (${response.status})`,
      body?.code ?? 'error',
      response.status,
    )
  }
  return body as T
}

export const api = {
  async session() {
    const body = await request<{
      csrfToken: string
      database: string
      undoable: Operation | null
    }>('/api/session')
    csrfToken = body.csrfToken
    return body
  },

  collection: (filters: Filters, opts: Filters = {}) =>
    request<CollectionPage>(`/api/collection${query(filters, opts)}`),

  insights: (filters: Filters) =>
    request<Insights>(`/api/collection/insights${query(filters)}`),

  bulkActions: () => request<BulkAction[]>('/api/bulk/actions'),

  bulkPreview: (selection: Selection) =>
    request<{
      count: number
      quantity: number
      valueCents: number
      value: string
      more: number
      sample: { title: string; edition: string; quantity: number; price: string }[]
    }>('/api/bulk/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(selection),
    }),

  bulkApply: (selection: Selection, action: string, value?: string) =>
    request<{ affected: number; summary: string }>('/api/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...selection, action, value }),
    }),

  imports: () => request<ImportRecord[]>('/api/imports'),

  upload: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{ importId: number; kind: string }>('/api/imports', {
      method: 'POST',
      body: form,
    })
  },

  importDetail: (id: number) => request<ImportDetail>(`/api/imports/${id}`),

  resolveRow: (importId: number, rowId: number, body: Record<string, unknown>) =>
    request<{ blocking: number }>(`/api/imports/${importId}/rows/${rowId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  commitImport: (id: number) =>
    request<{ added: number; updated: number; kind: string }>(
      `/api/imports/${id}/commit`,
      { method: 'POST' },
    ),

  discardImport: (id: number) =>
    request<{ discarded: number }>(`/api/imports/${id}/discard`, { method: 'POST' }),

  history: () => request<Operation[]>('/api/history'),

  undo: () => request<Operation>('/api/undo', { method: 'POST' }),
}

/** Exposed for tests; the app sets this via `api.session()`. */
export function __setToken(token: string) {
  csrfToken = token
}
