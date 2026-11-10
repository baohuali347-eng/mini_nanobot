import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发时只代理 API；正式构建后由同一个 Python 服务提供页面。
export default defineConfig({
  plugins: [react()],
  build: { outDir: '../mini_nanobot/web', emptyOutDir: true },
  server: { host: '127.0.0.1', proxy: {
    '/api': { target: process.env.MINI_API_URL || 'http://127.0.0.1:8765', changeOrigin: true },
  } },
});
