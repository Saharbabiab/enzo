import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server forwards /api and /ws to the backend, so the app always talks
// to its own origin — the same code works on the PC (dev) and behind HTTPS (phone).
// xfwd adds X-Forwarded-For so the backend knows a request came through a proxy.
// ENZO_BACKEND (env) points the proxy elsewhere, e.g. a test backend.
declare const process: { env: Record<string, string | undefined> }
const backend = process.env.ENZO_BACKEND || 'http://localhost:8765'

// https://vite.dev/config/
export default defineConfig({
    plugins: [react()],
    server: {
        port: 5173,
        host: true,
        proxy: {
            '/api': { target: backend, xfwd: true },
            '/ws': { target: backend, ws: true, xfwd: true },
        },
    },
})
