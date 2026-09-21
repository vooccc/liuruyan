import { API_BASE } from '../api/base'

/**
 * 解析用户消息正文末尾被 backend 拼上的"[附件 / 用户上传，请用 read_file 工具读取]" footer，
 * 抽出附件路径供 UI 单独渲染（图片缩略图，非图文件后续可加 chip）。
 *
 * 同步契约：footer 文案与 backend/main.py::chat() 顶部拼装逻辑完全一致——任何一边改动
 * 都要同步另一边，否则 UI 解析失败会让附件显示降级到原始路径文本（不影响 LLM 行为，
 * 仅 user-bubble 渲染丑）。
 *
 * 文件名 userAttachmentFooter.ts（不用 userMessageAttachments.ts）：
 * 后者会跟同目录的 UserMessageAttachments.tsx 在 Windows 大小写不敏感文件系统下冲突。
 */

const ATTACHMENT_HEADER = '[附件 / 用户上传，请用 read_file 工具读取]'
const _IMG_RE = /\.(png|jpe?g|gif|webp|bmp|svg|ico|tiff?|heic|heif)$/i

/**
 * 给用户消息文本拼上 attachment footer，供前端"乐观渲染"路径用。
 *
 * 文案与 backend/main.py::chat() 顶部拼装契约 1:1 同步——backend 自己做持久化时
 * 会基于 raw `message` 重新拼一遍同样格式 footer。所以前端 cache 这边提前拼，
 * 待 backend 回包/会话刷新后两边表示等价，user-bubble 不会出现"先空后有图"的跳变。
 *
 * 注意：此函数仅供 cache 显示层用。**不要**把它的返回值塞进发往 /chat 的 wire body
 * （`request.message`），否则会和 backend footer 拼接形成 footer×2。
 */
export function buildUserMessageWithAttachments(text: string, paths: string[] | undefined | null): string {
  const t = String(text || '')
  if (!paths || paths.length === 0) return t
  const footer = `\n\n${ATTACHMENT_HEADER}\n` + paths.map(p => `- ${p}`).join('\n')
  return t + footer
}

export interface UserMessageAttachment {
  /** 后端绝对路径（POST /chat/upload 返回的那个）。 */
  path: string
  /** basename，用于 alt / caption / hover 提示。 */
  name: string
  /** 是否图片（按扩展名判定）。仅图片走缩略图渲染，其它类型当前先不显示卡片。 */
  isImage: boolean
}

export interface ParsedUserMessageContent {
  /** 移除 footer 后用于显示的正文（保留前导/中段空行；末尾 trim）。 */
  cleanedText: string
  attachments: UserMessageAttachment[]
}

/**
 * 解析用户消息文本。没有 footer 时原样返。
 *
 * footer 形态：
 *
 *     <用户原文>\n\n[附件 / 用户上传，请用 read_file 工具读取]\n- /abs/path/a.png\n- /abs/path/b.pdf
 */
export function parseUserMessageAttachments(text: string | null | undefined): ParsedUserMessageContent {
  const raw = String(text || '')
  if (!raw) return { cleanedText: raw, attachments: [] }
  const idx = raw.indexOf(ATTACHMENT_HEADER)
  if (idx === -1) return { cleanedText: raw, attachments: [] }

  const before = raw.slice(0, idx).replace(/\s+$/, '')
  const footerPart = raw.slice(idx)
  // 跳过 header 这一行后的 path 列表
  const lines = footerPart.split('\n').slice(1)
  const attachments: UserMessageAttachment[] = []
  for (const line of lines) {
    const trimmed = line.trim()
    if (!trimmed.startsWith('-')) continue
    const path = trimmed.replace(/^-\s*/, '').trim()
    if (!path) continue
    const name = path.split(/[\\/]/).filter(Boolean).pop() || path
    attachments.push({
      path,
      name,
      isImage: _IMG_RE.test(name),
    })
  }

  return { cleanedText: before, attachments }
}

/**
 * 拼出 backend `/chat/upload/file` 端点 URL，给前端 <img src> 用。
 * 后端会校验 path 必须落在 uploads_dir 内，越界返 403。
 */
export function getUploadedFileUrl(path: string): string {
  return `${API_BASE}/chat/upload/file?path=${encodeURIComponent(path)}`
}
