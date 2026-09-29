import { useMemo, useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  Copy,
  FileDiff,
  FilePlus2,
  FileMinus2,
  FileEdit,
} from 'lucide-react'

/**
 * FileDiffCard — 在 chat 里展示 edit / multi_edit / write_to_file 工具的红/绿 diff。
 *
 * 协议：后端工具返回 result JSON 含 `_runtime_block: 'file_diff'` sentinel +
 * path / stats / hunks 字段。runtimeBlocks 识别后渲染本卡片，取代默认 tool_result。
 *
 * 同款"工具结果 sentinel"模式见：
 * - BrowserPreviewCard
 * - AskUserQuestionCard
 * - CommandStreamCard
 *
 * 视觉风格对齐 Cascade IDE 的 diff 块：
 * - 顶栏：文件图标 + 路径短显（hover 全路径）+ 统计徽章（+N -N）+ 创建/删除/修改标识 + 折叠按钮
 * - hunk 头部："@@ -old,count +new,count @@" 灰色字（git 风格）
 * - 行：左侧两列行号（old / new）+ 中间符号（+/-/空）+ 右侧 content
 *   - context: 项目正常背景
 *   - remove:  红色行（淡红底 + 深红字）
 *   - add:     绿色行（淡绿底 + 深绿字）
 * - 截断时底部 "diff 较大，已截断 N 行" 灰色提示
 */

export type FileDiffLineType = 'context' | 'add' | 'remove'

export interface FileDiffLine {
  type: FileDiffLineType
  content: string
  old_no: number | null
  new_no: number | null
}

export interface FileDiffHunk {
  old_start: number
  old_count: number
  new_start: number
  new_count: number
  lines: FileDiffLine[]
}

export interface FileDiffPayload {
  _runtime_block: 'file_diff'
  path: string
  stats: { added: number; removed: number }
  hunks: FileDiffHunk[]
  truncated: boolean
  is_create: boolean
  is_delete: boolean
  // 透传字段（不参与渲染，但 LLM 兼容）
  success?: boolean
  replacements?: number
  applied?: number
  lines_written?: number
}

function shortPath(path: string): string {
  // Windows / Unix 都按 / 或 \ 切，取末尾两段。绝对路径 d:\foo\bar\baz.txt → bar/baz.txt
  if (!path) return ''
  const parts = path.split(/[\\/]/).filter(Boolean)
  if (parts.length <= 2) return path
  return parts.slice(-2).join('/')
}

export function FileDiffCard({ payload }: { payload: FileDiffPayload }) {
  // 默认展开；用户点击折叠按钮收起。大 diff 默认折叠以免炸聊天屏
  const totalLines = useMemo(
    () => payload.hunks.reduce((acc, h) => acc + h.lines.length, 0),
    [payload.hunks],
  )
  const [collapsed, setCollapsed] = useState(totalLines > 80)
  const [pathCopied, setPathCopied] = useState(false)

  const onCopyPath = async () => {
    try {
      await navigator.clipboard.writeText(payload.path)
      setPathCopied(true)
      setTimeout(() => setPathCopied(false), 1500)
    } catch {
      // 剪贴板权限失败：兜底没动作
    }
  }

  // 操作类型判定：用于头栏图标 + 文案
  let opIcon = <FileEdit size={13} />
  let opLabel = '修改'
  let opClass = 'edit'
  if (payload.is_create) {
    opIcon = <FilePlus2 size={13} />
    opLabel = '新建'
    opClass = 'create'
  } else if (payload.is_delete) {
    opIcon = <FileMinus2 size={13} />
    opLabel = '删除'
    opClass = 'delete'
  }

  const hasChanges = payload.hunks.length > 0
  const added = payload.stats?.added ?? 0
  const removed = payload.stats?.removed ?? 0

  return (
    <div className="file-diff-card" data-op={opClass}>
      <div className="file-diff-toolbar">
        <button
          type="button"
          className="file-diff-collapse-btn"
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? '展开' : '折叠'}
          aria-label={collapsed ? '展开 diff' : '折叠 diff'}
        >
          {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
        </button>
        <span className={`file-diff-op-badge file-diff-op-${opClass}`} title={opLabel}>
          {opIcon}
          <span>{opLabel}</span>
        </span>
        <FileDiff size={13} className="file-diff-icon" />
        <span className="file-diff-path" title={payload.path}>
          {shortPath(payload.path)}
        </span>
        <div className="file-diff-stats">
          {added > 0 && <span className="file-diff-stat-add">+{added}</span>}
          {removed > 0 && <span className="file-diff-stat-remove">-{removed}</span>}
          {!hasChanges && <span className="file-diff-stat-noop">无变化</span>}
        </div>
        <button
          type="button"
          className="file-diff-copy-btn"
          onClick={onCopyPath}
          title="复制完整路径"
        >
          <Copy size={12} />
          {pathCopied && <span className="file-diff-copied-flash">已复制</span>}
        </button>
      </div>

      {!collapsed && hasChanges && (
        <div className="file-diff-hunks">
          {payload.hunks.map((hunk, hi) => (
            <div key={hi} className="file-diff-hunk">
              <div className="file-diff-hunk-header">
                @@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@
              </div>
              <table className="file-diff-table">
                <tbody>
                  {hunk.lines.map((line, li) => (
                    <tr
                      key={li}
                      className={`file-diff-row file-diff-row-${line.type}`}
                    >
                      <td className="file-diff-lineno file-diff-lineno-old">
                        {line.old_no ?? ''}
                      </td>
                      <td className="file-diff-lineno file-diff-lineno-new">
                        {line.new_no ?? ''}
                      </td>
                      <td className="file-diff-sign">
                        {line.type === 'add' ? '+' : line.type === 'remove' ? '-' : ' '}
                      </td>
                      <td className="file-diff-content">
                        <pre>{line.content || ' '}</pre>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
          {payload.truncated && (
            <div className="file-diff-truncated-note">
              diff 较大，已截断显示。完整内容请用 read_file 直接查看。
            </div>
          )}
        </div>
      )}

      {!collapsed && !hasChanges && (
        <div className="file-diff-noop-note">
          文件未发生变化（before == after）
        </div>
      )}
    </div>
  )
}

/** 尝试把 tool_result 解析成 FileDiff payload；不是则返 null。
 *
 * 协议：后端返 JSON 字符串含 `_runtime_block: 'file_diff'` sentinel。
 * 跟 BrowserPreviewCard / AskUserQuestionCard 同款。
 */
export function tryParseFileDiffPayload(
  raw: string | undefined | null,
): FileDiffPayload | null {
  if (!raw || typeof raw !== 'string') return null
  // 快速排除：不含 sentinel
  if (!raw.includes('"_runtime_block"') || !raw.includes('"file_diff"')) {
    return null
  }
  try {
    const obj = JSON.parse(raw)
    if (!obj || typeof obj !== 'object') return null
    if (obj._runtime_block !== 'file_diff') return null
    if (typeof obj.path !== 'string') return null
    if (!Array.isArray(obj.hunks)) return null
    return obj as FileDiffPayload
  } catch {
    return null
  }
}
