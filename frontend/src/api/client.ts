/**
 * The client facade. Components import from here and never know which backend
 * answers: Flask over HTTP (today's default) or the in-browser SQLite worker.
 *
 * `VITE_BACKEND=local` at build time selects the worker. The same frontend
 * running against both backends is the coexistence mechanism for the SPA port
 * — and the parity harness that gates retiring the server.
 */

import { localApi, pingLocal } from './transport-local'
import { httpApi } from './transport-http'
import type { Api } from './types'

export * from './types'
export { __setToken } from './transport-http'

const useLocal = import.meta.env.VITE_BACKEND === 'local'

export const api: Api = useLocal ? localApi : httpApi

if (useLocal && typeof window !== 'undefined') {
  // Debug/boot hook: lets a smoke script (and a curious devtools user) await
  // the worker's first answer and see which VFS actually mounted.
  ;(window as unknown as { __localBoot: Promise<unknown> }).__localBoot = pingLocal()
}
