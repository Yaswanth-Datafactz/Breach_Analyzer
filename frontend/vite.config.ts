import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Fixed port so the backend's CORS allowlist and docs/plan.md §7 stay
  // accurate (UC1 5173, UC2 5174, UC3 5175 -- one product family, no clashes).
  server: { port: 5175 },
})
