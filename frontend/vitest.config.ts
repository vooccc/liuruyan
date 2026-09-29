import { defineConfig } from 'vitest/config'

// 前端单测配置（与 vite.config.ts 分离，避免污染 build 链）。
// 用 jsdom 环境：useAppStore 在 DEV 下会挂 window.useAppStore（调试便利），
// node 环境无 window 会导致 import 即抛。jsdom 提供 window，且为后续 React
// 组件 / hook 测试预留环境。未来加 @testing-library/react 即可直接测组件。
export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    clearMocks: true,
    restoreMocks: true,
  },
})
