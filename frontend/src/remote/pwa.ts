/**
 * PWA Service Worker 注册（M8a）。
 *
 * 仅在 runtime.kind === 'web'（普通浏览器）下注册 SW——
 * Tauri 桌面壳 / VSCode webview 会被 runtime 工厂识别为非 'web'，自动跳过。
 *
 * SW 仅缓存 app shell，所有动态 API 不被接管。详见 frontend/public/sw.js。
 *
 * 入口：frontend/src/main.tsx 顶层调用 registerPWA()。
 *
 * 设计：_docs/REMOTE_M8A.md §5 Step 5
 * 隔离契约：.windsurf/rules/remote-isolation.md §二·内部纪律第 5 条
 */
import { getRuntime } from '../runtime'

export async function registerPWA(): Promise<void> {
  if (typeof window === 'undefined') return
  if (!('serviceWorker' in navigator)) return

  // runtime 探测：Tauri / VSCode webview 跳过；只有普通浏览器才注册 SW
  try {
    const runtime = await getRuntime()
    if (runtime.kind !== 'web') return
  } catch (err) {
    // runtime 探测异常保守跳过，不影响主应用启动
    // eslint-disable-next-line no-console
    console.warn('[remote/pwa] runtime probe failed, skip SW registration', err)
    return
  }

  try {
    await navigator.serviceWorker.register('/sw.js', { scope: '/' })
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn('[remote/pwa] SW registration failed', err)
  }
}
