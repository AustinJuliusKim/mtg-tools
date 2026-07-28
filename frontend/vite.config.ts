/// <reference types="vitest/config" />
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // `npm run dev` serves the UI on 5173 and proxies the API to Flask, so the
    // browser sees one origin and the session cookie behaves as in production.
    proxy: { '/api': { target: 'http://127.0.0.1:8765', changeOrigin: true } },
  },
  build: { outDir: 'dist', emptyOutDir: true },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test-setup.ts',
  },
})
