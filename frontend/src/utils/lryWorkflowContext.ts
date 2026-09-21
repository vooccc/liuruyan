/**
 * `.lry/` workflow 注入 helper（对应后端 `.lry/workflows/*.md` + `/api/lry/workflows`）。
 *
 * 工作原理：
 *   用户输入首行 `/<workflow-name>`（可跟参数），前端在 sendMessage 前：
 *   1) 解析首行命中的 workflow 名
 *   2) fetch `/api/lry/workflows/<name>` 拿内容
 *   3) 把 workflow 内容包进 `<details class="liuruyan-lry-workflow">` 折叠块
 *      拼到 user message 头部（在 IDE 上下文之前 / 之后都可接受；我们放在 ide 前面）
 *   4) 剔除首行 `/<name>` 保留剩余用户文本
 *
 * 为啥选 `<details>` 而不是自定义标签：
 *   跟 `ideContext.ts` 保持一致——标准 markdown 原生支持、transcript 持久化安全、
 *   用户消息气泡默认折叠显示一个 chip "📎 参考工作流：name"。
 *
 * 冲突策略：
 *   CHAT_SLASH_COMMANDS 优先（用户既有的 `/翻译` `/总结` 这些）。本 helper 只处理
 *   没命中内置 slash 的 `/xxx` 情况。如果 workflow 也不存在，静默返回 null，
 *   `/xxx` 会作为普通 user 文本正常发送。
 */
import { getLryWorkflow } from '../api/lry'

/** 首行只允许 `/slug` 或 `/slug 其余文本`，slug 必须字母开头 + 字母/数字/连字符 */
const WORKFLOW_SLUG_RE = /^\/([a-z][a-z0-9-]*)(?:[ \t]+([\s\S]*))?$/

/**
 * 从 rawText 识别首行 workflow 前缀。
 *
 * 返回值：
 *   - `null`：首行不是 `/slug` 格式 → 调用方走原流程
 *   - `{ name, rest }`：解析出 slug；rest 是剩余文本（可能跨行）
 *
 * 注意：这里只做格式识别，**不判断 workflow 是否真实存在**。真实性检查靠
 * `tryInjectLryWorkflow` 的 fetch 错误兜底（API 返回 404 → 静默降级）。
 */
export function detectLryWorkflowPrefix(
  rawText: string,
): { name: string; rest: string } | null {
  if (!rawText || !rawText.startsWith('/')) return null

  const newlineIdx = rawText.indexOf('\n')
  const firstLine = newlineIdx >= 0 ? rawText.slice(0, newlineIdx) : rawText
  const afterNewline = newlineIdx >= 0 ? rawText.slice(newlineIdx + 1) : ''

  const match = firstLine.match(WORKFLOW_SLUG_RE)
  if (!match) return null

  const name = match[1]
  const sameLineRest = match[2] ?? ''
  const rest = afterNewline
    ? sameLineRest
      ? `${sameLineRest}\n${afterNewline}`
      : afterNewline
    : sameLineRest
  return { name, rest: rest.trim() }
}

/**
 * 把 workflow name + content 包进 markdown `<details>` 折叠块。
 *
 * 设计要点：
 *   - class="liuruyan-lry-workflow" → 若将来想做 chip 样式有 hook 位
 *   - summary 文字包含 📎 表情，方便用户一眼识别
 *   - 块尾部留两个换行，跟后续内容隔开（跟 ideContext 风格一致）
 */
export function buildLryWorkflowBlock(name: string, content: string): string {
  return [
    '<details class="liuruyan-lry-workflow">',
    `<summary>📎 参考工作流：${name}</summary>`,
    '',
    content.trim(),
    '',
    '</details>',
    '',
    '',
  ].join('\n')
}

/**
 * 完整入口：检测 + fetch + 构造 block。
 *
 * 返回：
 *   - `null`：首行不是 `/slug` 格式，或 fetch 失败（workflow 不存在 / 网络错）
 *   - `{ block, rest, name }`：block 是可以直接拼到 user text 前的 markdown 串，
 *     rest 是剥掉首行后的用户剩余文本
 *
 * 失败一律静默降级到 null，不抛错——`/xxx` 有可能就是用户想发的普通文本。
 */
export async function tryInjectLryWorkflow(
  rawText: string,
): Promise<{ block: string; rest: string; name: string } | null> {
  const detected = detectLryWorkflowPrefix(rawText)
  if (!detected) return null

  try {
    const wf = await getLryWorkflow(detected.name)
    if (!wf || !wf.content) return null
    return {
      block: buildLryWorkflowBlock(detected.name, wf.content),
      rest: detected.rest,
      name: detected.name,
    }
  } catch {
    // 404 / 网络错 → 不是 workflow，调用方会拿原 rawText 走正常发送流程
    return null
  }
}

/**
 * 把 `buildLryWorkflowBlock` 生成的块从字符串里剥掉（给渲染时折叠 summary 用）。
 *
 * 用法：跟 `stripIdeContextBlock` 类似，在气泡渲染前剥。transcript/发给后端的
 * content 不受影响，保持完整 workflow 注入。
 *
 * 当前暂未接入——默认 `<details>` 本身就是折叠的，summary 小条已能区分；保留函数
 * 是给后续如果想彻底隐藏参考块的场景留口子。
 */
export function stripLryWorkflowBlock(content: string): string {
  if (!content || !content.includes('📎 参考工作流')) return content
  return content.replace(
    /<details class="liuruyan-lry-workflow">[\s\S]*?<\/details>\s*\n*/g,
    '',
  )
}
