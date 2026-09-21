import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // 5173 is deliberately avoided: another local dev server already holds it on
    // IPv4, and Vite would silently bind ::1 instead - leaving two different
    // servers answering "localhost:5173" depending on how the name resolved.
    host: '127.0.0.1',
    port: 5180,
    strictPort: true,
    proxy: {
      // The analysis API and its websocket. Proxied so the page is same-origin
      // in dev and no CORS config has to exist for localhost.
      // REST only. There is deliberately no '/ws' entry: adding a `ws: true`
      // proxy makes Vite install an upgrade handler that swallowed its own HMR
      // socket, so every websocket to the dev server failed. The live feed
      // connects straight to the service instead - see wsBase() in lib/api.ts.
      '/api': { target: 'http://127.0.0.1:8770', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true, chunkSizeWarningLimit: 900 },
})
