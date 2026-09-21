/**
 * Runtime 工厂 —— 按环境探测加载对应实现。
 *
 * 探测优先级：
 *   1. Tauri  — window.__TAURI_INTERNALS__ 或 window.__TAURI__ 存在
 *   2. VSCode — URL query 带 csrf + bridgePort（由插件 webview 注入）
 *   3. Web    — 兜底
 *
 * 探测结果缓存为单例，整个进程生命周期一致。
 */

import type { Runtime } from './types'

let _runtimePromise: Promise<Runtime> | null = null

export function getRuntime(): Promise<Runtime> {
  if (!_runtimePromise) {
    _runtimePromise = createRuntime()
  }
  return _runtimePromise
}

async function createRuntime(): Promise<Runtime> {
  if (typeof window !== 'undefined') {
    const w = window as unknown as { __TAURI_INTERNALS__?: unknown; __TAURI__?: unknown }
    if (w.__TAURI_INTERNALS__ || w.__TAURI__) {
      const { createTauriRuntime } = await import('./tauri')
      return createTauriRuntime()
    }
    const sp = new URLSearchParams(window.location.search)
    // VSCode 环境信号：插件 webview/html.ts 在 iframe src 里同时注入 csrf + bridgePort
    if (sp.has('csrf') && sp.has('bridgePort')) {
      const { createVSCodeRuntime } = await import('./vscode')
      return createVSCodeRuntime()
    }
  }
  const { createWebRuntime } = await import('./web')
  return createWebRuntime()
}

export type {
  Runtime,
  RuntimeKind,
  RuntimeWindow,
  RuntimeDialog,
  RuntimeFs,
  RuntimeShell,
  RuntimeIde,
  DiagnosticItem,
} from './types'
