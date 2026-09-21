/**
 * IDE 上下文注入 helper（C-1）。
 *
 * 工作原理：
 *   - sendMessage 前拍一张 IDE 快照（active editor 路径 + 选中起止行 + 选中文本片段）
 *   - 拼成 markdown `<details><summary>🪟 IDE 上下文</summary>...</details>` 块塞到 message 头部
 *   - 模型看到完整 metadata（HTML/markdown 都能读），用户在聊天气泡里看到的是一个折叠小标签
 *
 * 为啥用 <details> 而不是自定义 <ide_context> 标签：
 *   - 标准 markdown / react-markdown / 任何 markdown 渲染器都原生渲染 <details>
 *   - 不用改 RuntimeChainSegments / EnhancedTextBlock 等任何渲染逻辑
 *   - transcript 持久化也安全（HTML 字符串保留得住）
 *
 * 轻量原则：不拼全文，不超过 ~500 字符。模型需要看代码自己调 read_file（柳如烟 backend 有）。
 */

import type { Runtime } from '../runtime'

const STORAGE_KEY = 'liuruyan.ideContextEnabled'

/**
 * 把 buildIdeContextBlock 生成的 `<details>...🪟 IDE 上下文...</details>` 块从字符串里剥掉。
 *
 * 为什么需要：
 *   - 我们把 IDE 上下文拼在 user message.content 头部，让 backend / LLM 能看到
 *   - 但前端 chat 气泡里渲染时，<details> 折叠块的 summary 行"🪟 IDE 上下文..."依然
 *     一直显示，每条消息头上挂一行视觉噪音
 *   - 所以渲染前用这个函数把整块拿掉。transcript / 发给后端的 content 不受影响。
 *
 * 匹配策略：定位 summary 文本含 "🪟 IDE 上下文" 的 <details>...</details> 块（含后续空行）。
 * 找不到就原样返回（幂等、对 assistant 消息无副作用）。
 */
export function stripIdeContextBlock(content: string): string {
  if (!content || !content.includes('🪟 IDE 上下文')) return content
  return content.replace(
    /<details>\s*<summary>🪟 IDE 上下文[^<]*<\/summary>[\s\S]*?<\/details>\s*\n*/g,
    '',
  )
}

/** 默认开启；用户可在 IdeStatusBar 关。 */
export function isIdeContextEnabled(): boolean {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v === null) return true
    return v === '1' || v === 'true'
  } catch {
    return true
  }
}

export function setIdeContextEnabled(on: boolean): void {
  try {
    localStorage.setItem(STORAGE_KEY, on ? '1' : '0')
    // 同窗口订阅靠 storage 事件触发不了（同窗口不发 storage event），
    // 用一个自定义 event 让 IdeStatusBar 等订阅方实时响应。
    window.dispatchEvent(new CustomEvent('liuruyan:ideContextToggle', { detail: { on } }))
  } catch {
    /* localStorage 不可用就静默失败，下次会回退到默认 true */
  }
}

const MAX_SELECTED_TEXT_CHARS = 600

function trimSelectedText(text: string): string {
  if (text.length <= MAX_SELECTED_TEXT_CHARS) return text
  return text.slice(0, MAX_SELECTED_TEXT_CHARS) + `\n... (truncated, ${text.length - MAX_SELECTED_TEXT_CHARS} chars omitted)`
}

/** 0-indexed offset → 1-indexed line number（VSCode UI 风格，给模型读起来更自然） */
function offsetToLine(content: string, offset: number): number {
  if (offset <= 0) return 1
  let line = 1
  for (let i = 0; i < offset && i < content.length; i += 1) {
    if (content[i] === '\n') line += 1
  }
  return line
}

/**
 * 调 runtime.ide 拿当前 IDE 状态，返回 markdown 折叠块字符串。
 * 不可用 / 关闭 / 无 active editor 时返回空串。
 */
export async function buildIdeContextBlock(runtime: Runtime): Promise<string> {
  if (runtime.kind !== 'vscode' || !runtime.ide) return ''
  if (!isIdeContextEnabled()) return ''

  let editor: Awaited<ReturnType<typeof runtime.ide.getActiveEditor>> = null
  try {
    editor = await runtime.ide.getActiveEditor()
  } catch {
    return ''
  }
  if (!editor) return ''

  const lines: string[] = []
  lines.push(`Active File: ${editor.uri}`)
  if (editor.selection) {
    const startLine = offsetToLine(editor.content, editor.selection.start)
    const endLine = offsetToLine(editor.content, editor.selection.end)
    lines.push(`Selected (line ${startLine}-${endLine}):`)
    lines.push('```')
    lines.push(trimSelectedText(editor.selection.text))
    lines.push('```')
  } else {
    lines.push('Selection: (none)')
  }

  const body = lines.join('\n')
  // class="liuruyan-ide-ctx" → 让前端 CSS 把它做成窄条 chip（默认折叠 + 小字 + 灰色）。
  // 不剥离 / 不隐藏：开发者偶尔想点开看注入了什么；正常聊天又不抢视觉。
  // summary 改短："IDE"；放最前面整块占一行小条。
  return [
    '<details class="liuruyan-ide-ctx">',
    '<summary>🪟 IDE 上下文</summary>',
    '',
    body,
    '',
    '</details>',
    '',
    '',
  ].join('\n')
}
