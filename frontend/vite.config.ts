import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  build: {
    // 明确 ES 目标——vite 不设的话 ESBuild 默认是 'esnext'，老内核浏览器
    // （Android WebView < 80 / iOS Safari < 13）会在 parse bundle 时 SyntaxError 黑屏。
    // es2020 覆盖 2019 年后所有主流浏览器，是"现代特性 + 老内核兼容"的最佳平衡点。
    // 想再向下兼容到 ES5 时考虑上 @vitejs/plugin-legacy。
    target: 'es2020',
    // ⚠️ 不要再手写 manualChunks！
    //
    // 历史教训（M8a 远程访问首次暴露）：之前这里写过：
    //   if (id.includes('lucide-react')) return 'ui-vendor'
    //   if (id.includes('react'))        return 'react-vendor'
    //   return 'vendor'
    // 看似合理，实际埋了三角循环依赖的雷：
    //   - lucide-react 内部用 React.memo —— 进 ui-vendor，跨 chunk 引用 react-vendor 里的 React
    //   - scheduler / use-sync-external-store 这些 React 运行时依赖名字不含 'react'
    //     → 被分到 vendor chunk —— 但 React 又依赖它们
    //   - 形成 react-vendor ↔ vendor ↔ ui-vendor 三角依赖，prod build 下浏览器原生 ESM
    //     加载触发"循环依赖未初始化"race → 访问到 undefined.memo → 全站黑屏
    //
    // 桌面 Tauri 时一直没暴露是因为 Tauri webview 的 ESM 加载链路跟浏览器原生 HTTP
    // 加载机制有微妙差异，"碰巧"避开 race；M8a 让浏览器直接吃 dist 是第一次暴露。
    //
    // 修法：删掉 manualChunks，让 vite/rollup 自动分块——它知道依赖图，比手写的
    // 字符串 includes 判断稳得多。如果以后真要控制 chunk 大小，请用 vite 推荐的
    // build.chunkSizeWarningLimit 调警告阈值，或者用 splitVendorChunkPlugin。
    //
    // 不要因为"我觉得这样分更直观"就再加回来——直观和正确是两回事。
  },
  server: {
    port: 1420,
    strictPort: true,
    watch: {
      ignored: ['**/src-tauri/target/**'],
    },
  },
})
