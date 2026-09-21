/**
 * Tauri runtime 实现 —— 包装现有 @tauri-apps/api / plugin-dialog / plugin-fs。
 *
 * 实现策略：动态 import，避免在非 Tauri 环境（VSCode webview / 纯浏览器）
 * 引入 Tauri 包导致 build 警告。仅当工厂判定 kind === 'tauri' 时才加载。
 */

import type { Runtime, RuntimeWindow, RuntimeDialog, RuntimeFs, RuntimeShell } from './types'

export async function createTauriRuntime(): Promise<Runtime> {
  const { getCurrentWindow } = await import('@tauri-apps/api/window')
  const { invoke } = await import('@tauri-apps/api/core')
  const tauriWindow = getCurrentWindow()

  const windowImpl: RuntimeWindow = {
    minimize: () => tauriWindow.minimize(),
    toggleMaximize: () => tauriWindow.toggleMaximize(),
    isMaximized: () => tauriWindow.isMaximized(),
    close: () => tauriWindow.close(),
    startDragging: () => tauriWindow.startDragging(),
    async onResized(cb) {
      const unlisten = await tauriWindow.onResized(() => cb())
      return unlisten
    },
  }

  const dialogImpl: RuntimeDialog = {
    async save(options) {
      const { save } = await import('@tauri-apps/plugin-dialog')
      const result = await save({
        defaultPath: options.defaultPath,
        filters: options.filters,
      })
      return result ?? null
    },
    async pickDirectory(options) {
      const { open } = await import('@tauri-apps/plugin-dialog')
      // directory: true → 选文件夹模式；用户取消时返回 null
      const result = await open({
        directory: true,
        multiple: false,
        defaultPath: options?.defaultPath,
        title: options?.title,
      })
      // open() 返回值可能是 string | string[] | null；directory + multiple:false 时是 string
      if (typeof result === 'string') return result
      return null
    },
  }

  const fsImpl: RuntimeFs = {
    async copyFile(source, target) {
      const fs = await import('@tauri-apps/plugin-fs')
      await fs.copyFile(source, target)
    },
  }

  const shellImpl: RuntimeShell = {
    async openExternal(url) {
      // Tauri 端调 Rust custom command（main.rs::open_external），转给系统 shell.open。
      // Rust 端做了 URL scheme 白名单校验，不安全 scheme 会返回 Err。
      try {
        await invoke('open_external', { url })
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn('[runtime/tauri] open_external failed', err)
      }
    },
  }

  return {
    kind: 'tauri',
    window: windowImpl,
    dialog: dialogImpl,
    fs: fsImpl,
    shell: shellImpl,
    ide: null,
  }
}
