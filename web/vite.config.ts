import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发时 /api 转给 demo/server.py；打包后由它直接托管 dist
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:8095' } },
})
