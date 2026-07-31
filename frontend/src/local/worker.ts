/**
 * The "local server": a dedicated worker that owns the SQLite database.
 *
 * VFS is `opfs-sahpool` — sync access handles inside this worker, no
 * COOP/COEP headers, works on any static host, and its one-open-handle rule
 * matches the app's single-writer design (Flask had one writer too; here the
 * worker is the writer). If OPFS is unavailable (old browser, some private
 * modes, non-worker test contexts), we fall back to an in-memory database and
 * say so in `ping` — the UI can then warn that nothing persists.
 *
 * Phase 0: boots, runs the schema (the exact `webapp/schema.sql`, imported as
 * text), answers `ping`. Every other route answers `not-implemented` until
 * its phase ports it — the route table below is the port's checklist.
 */

import sqlite3InitModule from '@sqlite.org/sqlite-wasm'
import schemaSql from '../../../webapp/schema.sql?raw'
import { initSchema, transaction, type Database } from './db'
import { ApiFailure } from './errors'
import {
  UndoLookupError,
  latestUndoable,
  recent,
  undoOperation,
} from './operations'
import type { PingResult, RpcRequest, RpcResponse } from './rpc'

let db: Database | null = null
let vfs: PingResult['vfs'] = 'memory'

async function boot(): Promise<void> {
  const sqlite3 = await sqlite3InitModule()
  try {
    const poolUtil = await sqlite3.installOpfsSAHPoolVfs({})
    db = new poolUtil.OpfsSAHPoolDb('/collection.db') as unknown as Database
    vfs = 'opfs-sahpool'
  } catch {
    db = new sqlite3.oo1.DB(':memory:') as unknown as Database
    vfs = 'memory'
  }
  initSchema(db, schemaSql)
}

const ready = boot()

type Handler = (payload: never) => unknown

const routes: Record<string, Handler | null> = {
  ping: () => ({
    status: 'ok',
    vfs,
    schemaVersion: db!.selectValue('SELECT version FROM schema_version'),
  }),
  // Phase 1
  session: () => ({
    csrfToken: '', // no server, no cookies, nothing for CSRF to defend
    database: vfs === 'opfs-sahpool' ? 'opfs:/collection.db' : ':memory: (nothing persists)',
    undoable: latestUndoable(db!),
  }),
  history: () => recent(db!, 50),
  undo: () => {
    try {
      return transaction(db!, () => undoOperation(db!))
    } catch (error) {
      if (error instanceof UndoLookupError) {
        throw new ApiFailure(error.message, 'nothing-to-undo', 409)
      }
      throw error
    }
  },
  // Phase 2
  collection: null,
  insights: null,
  sealed: null,
  sealedInsights: null,
  bulkActions: null,
  // Phase 3
  imports: null,
  upload: null,
  importDetail: null,
  resolveRow: null,
  commitImport: null,
  discardImport: null,
  // Phase 4
  bulkPreview: null,
  bulkApply: null,
  salesQueue: null,
  sales: null,
  salesSummary: null,
  listForSale: null,
  recordSale: null,
  cancelSale: null,
  // Phase 5
  exportManifest: null,
  buylistSummary: null,
  download: null,
}

self.onmessage = async (event: MessageEvent<RpcRequest>) => {
  const { id, route, payload } = event.data
  let response: RpcResponse
  try {
    await ready
    const handler = routes[route]
    if (!handler) {
      response = {
        id,
        ok: false,
        error: `'${route}' is not ported to the local backend yet.`,
        code: 'not-implemented',
        status: 501,
      }
    } else {
      response = { id, ok: true, result: await handler(payload as never) }
    }
  } catch (error) {
    response = {
      id,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
      code: error instanceof ApiFailure ? error.code : 'error',
      status: error instanceof ApiFailure ? error.status : 500,
    }
  }
  self.postMessage(response)
}
