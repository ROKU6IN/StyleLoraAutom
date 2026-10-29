import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: process.env.STYLELORA_API
      ? {
          '/api': { target: process.env.STYLELORA_API, changeOrigin: true },
        }
      : undefined,
  },
  build: { sourcemap: false, assetsInlineLimit: 0 },
});
