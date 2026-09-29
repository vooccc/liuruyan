/**
 * Web (浏览器) runtime —— 兜底实现。
 *
 * 浏览器没有窗口 chrome 控制能力，相关 API 全部 no-op；fs / dialog 抛清晰错误，
 * 让调用方明确知道这条路径在浏览器跑不通（通常调用方会先检查 runtime.kind）。
 */

import type { Runtime, RuntimeWindow, RuntimeDialog, RuntimeFs, RuntimeShell } from './types'

const notAvailable = (api: string) =>
  new Error(`Runtime API not available in web mode: ${api}`)

const webWindow: RuntimeWindow = {
  async minimize() {
    /* no-op */
  },
  async toggleMaximize() {
    /* no-op */
  },
  async isMaximized() {
    return false
  },
  async close() {
    /* no-op */
  },
  async startDragging() {
    /* no-op */
  },
  async onResized(_cb) {
    // 浏览器有 window.addEventListener('resize', ...)，但 chrome 状态不会变。
    // 阶段 1 不接入，unlisten 返回 no-op 函数。
    return () => {
      /* no-op */
    }
  },
}

const webDialog: RuntimeDialog = {
  async save(_options) {
    throw notAvailable('dialog.save')
  },
  async pickDirectory(_options) {
    throw notAvailable('dialog.pickDirectory')
  },
}

const webFs: RuntimeFs = {
  async copyFile(_source, _target) {
    throw notAvailable('fs.copyFile')
  },
}

const webShell: RuntimeShell = {
  async openExternal(url) {
    // 浏览器：新 tab 打开。noopener,noreferrer 切断 opener 引用，防钓鱼。
    try {
      window.open(url, '_blank', 'noopener,noreferrer')
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn('[runtime/web] window.open failed', err)
    }
  },
}

export async function createWebRuntime(): Promise<Runtime> {
  return {
    kind: 'web',
    window: webWindow,
    dialog: webDialog,
    fs: webFs,
    shell: webShell,
    ide: null,
  }
}
