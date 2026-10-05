import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    include: ['src/**/*.{test,spec}.{js,ts}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
      exclude: ['node_modules/', 'src/main.ts', '**/*.d.ts'],
    },
    // jsdom 缺 window.matchMedia 等少量浏览器 API，产品代码里合法使用它们，
    // 补在测试侧（见 src/test-setup.ts）
    setupFiles: ['./src/test-setup.ts'],
  },
})
