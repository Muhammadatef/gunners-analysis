import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/season': 'http://127.0.0.1:8000',
      '/matches': 'http://127.0.0.1:8000',
      '/seasons': 'http://127.0.0.1:8000',
      '/clubs': 'http://127.0.0.1:8000',
      '/players': 'http://127.0.0.1:8000',
      '/output': 'http://127.0.0.1:8000',
      '/pipeline': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
})
