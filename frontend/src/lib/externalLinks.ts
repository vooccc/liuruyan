/**
 * 全局外部链接拦截器。
 *
 * 核心设计：把 openExternal 抽到 runtime 层（runtime.shell.openExternal），
 * 各 runtime 各自实现（Tauri invoke / VSCode 走 webview / 浏览器 window.open）。
 * 此处只负责 document 级 capture click 拦截裸 `<a>`（markdown 渲染产物），
 * 把"用哪种方式打开"完全交给 runtime——业务零环境嗅探。
 *
 * 背景：角色发的 markdown 链接在 Tauri webview / VSCode webview 这种"单 frame 容器"里，
 * 不拦截就会被当前 webview 直接导航过去，整个柳如烟界面被替换且回不来。
 */

import { getRuntime } from '../runtime'

let installed = false

function isExternalHref(href: string | null | undefined): boolean {
  if (!href) return false
  return /^(https?:|mailto:|tel:)/i.test(href.trim())
}

async function openExternal(url: string): Promise<void> {
  try {
    const runtime = await getRuntime()
    await runtime.shell.openExternal(url)
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error('[externalLinks] openExternal failed', err)
  }
}

export function installExternalLinkInterceptor(): void {
  if (installed) return
  installed = true

  // 用 capture 阶段，赶在 React 合成事件之前拦下来。
  document.addEventListener(
    'click',
    (e) => {
      // 用户按住修饰键想自己控制行为时不拦（比如 ctrl+click 已知行为）
      if (e.defaultPrevented) return
      const path = (e.composedPath?.() ?? []) as EventTarget[]
      let anchor: HTMLAnchorElement | null = null
      for (const node of path) {
        if (node instanceof HTMLAnchorElement) {
          anchor = node
          break
        }
      }
      if (!anchor) {
        // 兜底：从 target 往上找
        const t = e.target as HTMLElement | null
        anchor = t?.closest?.('a[href]') as HTMLAnchorElement | null
      }
      if (!anchor) return

      const href = anchor.getAttribute('href')
      if (!isExternalHref(href)) return

      // 同 frame 内 hash 跳转 / 内部 router 链接不动
      e.preventDefault()
      e.stopPropagation()
      void openExternal(href!)
    },
    true,
  )
}
