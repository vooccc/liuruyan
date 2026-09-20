import { useEffect, useMemo, useState } from 'react'
import { Edit3, Search, Trash2, UsersRound } from 'lucide-react'
import {
  listSessions,
  deleteSession,
  updateSessionTitle,
  type Session,
  type Agent,
  type SceneInfo,
} from '../api/chat'
import { COLLAB_SCENE_CONFIGS } from './collabUiText'

import AgentAvatar from './AgentAvatar'
import { getAvatarSrc } from './avatarUtils'
import { useConfirmDialog } from './ConfirmDialog'
import { startPolling } from '../lib/pollingLoop'
import { useResizableWidth, ResizeHandle } from './useResizableWidth'
import { CollabLauncher } from './CollabLauncher'

interface RoundtableListProps {
  agents: Agent[]
  /** 当前在协作视图中打开的协作 session */
  activeCollabSessionId?: string
  /** 进行中（正在流式响应）的会话 ID 集合 */
  runningSessionIds?: Set<string>
  /** 点击某条协作 session */
  onOpenCollab: (sessionId: string, metadata: Record<string, unknown>) => void
  /** 创建新协作后的跳转回调（来自 CollabLauncher） */
  onCollabCreated?: (scene: SceneInfo, sessionId?: string) => void
  /** 定时外部刷新时机（毫秒） */
  refreshIntervalMs?: number
}

interface CollabItem {
  session: Session
  sceneType?: string
  participants: string[]
  updatedAtMs: number
  isRunning: boolean
}

/**
 * 议事厅列表 —— 协作视图专用侧栏内容。
 *
 * 设计要点：
 *   · 顶部一行标题 + 搜索（轻量）
 *   · 进行中分组优先展示（左侧脉冲光点）
 *   · 每条卡片：类型标签 + 标题 + 头像堆 + 时间
 *   · 极简大方，工作化，配色呼应 App icon 暖金+冰蓝
 */
export default function RoundtableList({
  agents,
  activeCollabSessionId,
  runningSessionIds,
  onOpenCollab,
  onCollabCreated,
  refreshIntervalMs = 10000,
}: RoundtableListProps) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [search, setSearch] = useState('')
  const { confirm: confirmAction, dialog: confirmDialog } = useConfirmDialog()

  // 议事厅侧栏宽度可拖拽调整
  const { width: panelWidth, handleProps: panelResizeProps } = useResizableWidth({
    storageKey: 'lr:roundtable-panel-width',
    defaultWidth: 280,
    min: 220,
    max: 480,
  })

  // 初次 + 定时拉取
  const refreshSessions = async () => {
    try {
      const resp = await listSessions()
      setSessions(resp.sessions)
    } catch { /* silent */ }
  }
  useEffect(() => {
    return startPolling(refreshSessions, refreshIntervalMs)
  }, [refreshIntervalMs])

  const items: CollabItem[] = useMemo(() => {
    const q = search.trim().toLowerCase()
    return sessions
      .map((session): CollabItem | null => {
        const meta = session.metadata || {}
        const collabType = (meta.collab_type as string | undefined) || ''
        if (collabType !== 'scene') return null
        const rawParticipants = Array.isArray(meta.participants) ? meta.participants : []
        const participants = rawParticipants.filter((p): p is string => typeof p === 'string')
        return {
          session,
          sceneType: typeof meta.scene_type === 'string' ? (meta.scene_type as string) : undefined,
          participants,
          updatedAtMs: new Date(session.updatedAt).getTime() || 0,
          isRunning: runningSessionIds?.has(session.id) || false,
        }
      })
      .filter((x): x is CollabItem => !!x)
      .filter((x) => {
        if (!q) return true
        const t = x.session.title.toLowerCase()
        if (t.includes(q)) return true
        for (const pid of x.participants) {
          const agent = agents.find((a) => a.id === pid)
          if ((agent?.name || pid).toLowerCase().includes(q)) return true
        }
        return false
      })
      .sort((a, b) => {
        if (a.isRunning !== b.isRunning) return a.isRunning ? -1 : 1
        return b.updatedAtMs - a.updatedAtMs
      })
  }, [sessions, search, agents, runningSessionIds])

  const runningItems = items.filter((x) => x.isRunning)
  const archivedItems = items.filter((x) => !x.isRunning)

  async function handleDelete(sessionId: string, e: React.MouseEvent) {
    e.stopPropagation()
    const ok = await confirmAction('删除协作', '确定删除此次协作记录？此操作不可恢复。', 'danger', '删除')
    if (!ok) return
    try {
      await deleteSession(sessionId)
      setSessions((prev) => prev.filter((s) => s.id !== sessionId))
    } catch {}
  }

  // 重命名状态与 Sidebar 同样的两变量双锅：id + draft
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editingTitle, setEditingTitle] = useState('')

  function startRename(session: Session, e: React.MouseEvent) {
    e.stopPropagation()
    setEditingId(session.id)
    setEditingTitle(session.title)
  }

  async function commitRename(sessionId: string) {
    const next = editingTitle.trim()
    if (!next) { setEditingId(null); return }
    try {
      const updated = await updateSessionTitle(sessionId, next)
      setSessions((prev) => prev.map((s) => s.id === sessionId ? updated : s))
    } catch { /* silent */ }
    setEditingId(null)
  }

  return (
    <div
      className="roundtable-panel"
      aria-label="议事厅会话列表"
      style={{ ['--roundtable-panel-width' as string]: `${panelWidth}px` } as React.CSSProperties}
    >
      {/* ===== 顶部头：标题 + 搜索 ===== */}
      <div className="roundtable-header">
        <div className="roundtable-title">
          <UsersRound size={15} className="roundtable-title-icon" />
          <span>议事厅</span>
          {items.length > 0 && (
            <span className="roundtable-title-count">{items.length}</span>
          )}
        </div>
        {/* 议事厅作为协作中心：发起协作主入口 */}
        <div className="roundtable-launch">
          <CollabLauncher
            agents={agents}
            onCreated={(scene, sid) => { refreshSessions(); onCollabCreated?.(scene, sid) }}
            triggerVariant="primary"
            triggerLabel="发起协作"
          />
        </div>
      </div>

      <div className="roundtable-search">
        <Search size={14} className="roundtable-search-icon" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="搜索协作 / 参与者"
          className="roundtable-search-input"
        />
      </div>

      {/* ===== 列表主体 ===== */}
      <div className="roundtable-list">
        {runningItems.length > 0 && (
          <div className="roundtable-group">
            <div className="roundtable-group-label">
              <span className="roundtable-group-dot roundtable-group-dot-live" />
              进行中
            </div>
            {runningItems.map((item) => (
              <CollabCard
                key={item.session.id}
                item={item}
                agents={agents}
                active={item.session.id === activeCollabSessionId}
                panelWidth={panelWidth}
                editing={editingId === item.session.id}
                editingTitle={editingTitle}
                onEditingTitleChange={setEditingTitle}
                onStartRename={(e) => startRename(item.session, e)}
                onCommitRename={() => commitRename(item.session.id)}
                onCancelRename={() => setEditingId(null)}
                onOpen={() => onOpenCollab(item.session.id, item.session.metadata || {})}
                onDelete={(e) => handleDelete(item.session.id, e)}
              />
            ))}
          </div>
        )}

        {archivedItems.length > 0 && (
          <div className="roundtable-group">
            {runningItems.length > 0 && (
              <div className="roundtable-group-label">
                <span className="roundtable-group-dot" />
                最近
              </div>
            )}
            {archivedItems.map((item) => (
              <CollabCard
                key={item.session.id}
                item={item}
                agents={agents}
                active={item.session.id === activeCollabSessionId}
                panelWidth={panelWidth}
                editing={editingId === item.session.id}
                editingTitle={editingTitle}
                onEditingTitleChange={setEditingTitle}
                onStartRename={(e) => startRename(item.session, e)}
                onCommitRename={() => commitRename(item.session.id)}
                onCancelRename={() => setEditingId(null)}
                onOpen={() => onOpenCollab(item.session.id, item.session.metadata || {})}
                onDelete={(e) => handleDelete(item.session.id, e)}
              />
            ))}
          </div>
        )}

        {items.length === 0 && (
          <div className="roundtable-empty">
            <div className="roundtable-empty-icon" aria-hidden="true">
              <svg viewBox="0 0 48 48" width="46" height="46">
                <g fill="none" stroke="currentColor" strokeWidth="1.4" opacity="0.55">
                  <circle cx="19" cy="24" r="11" />
                  <circle cx="29" cy="24" r="11" />
                </g>
              </svg>
            </div>
            <div className="roundtable-empty-title">尚无协作</div>
            <div className="roundtable-empty-desc">
              {search.trim()
                ? '没有匹配的协作记录'
                : '选择参与者，开启一场多人协作'}
            </div>
            {!search.trim() && (
              <div className="roundtable-empty-cta">
                <CollabLauncher
                  agents={agents}
                  onCreated={(scene, sid) => { refreshSessions(); onCollabCreated?.(scene, sid) }}
                  triggerVariant="primary"
                  triggerLabel="+ 发起第一场协作"
                />
              </div>
            )}
          </div>
        )}
      </div>

      {confirmDialog}

      {/* 右边缘拖拽调宽手柄（双击重置） */}
      <ResizeHandle {...panelResizeProps} />
    </div>
  )
}

/* ========== 协作卡片 ========== */

interface CollabCardProps {
  item: CollabItem
  agents: Agent[]
  active: boolean
  /** 当前面板像素宽度，用于按宽度计算最多展示几个头像 */
  panelWidth: number
  /** 当前是否在重命名编辑态 */
  editing: boolean
  editingTitle: string
  onEditingTitleChange: (next: string) => void
  onStartRename: (e: React.MouseEvent) => void
  onCommitRename: () => void
  onCancelRename: () => void
  onOpen: () => void
  onDelete: (e: React.MouseEvent) => void
}

/** 按面板宽度估算 row2 还能塞下几个头像。
 *  几何参考：
 *    - row2 横向预算 ≈ panelWidth − 24（list 左右各 12 padding）
 *                       − 26（卡片自身 12+14 padding）
 *                       − 38（类型 tag 估宽）
 *                       − 60（"XX 分钟前" 时间估宽，留余量）
 *                       − 16（两个 8px gap）
 *    - 第一个头像占 20px，后续每个因 -7px 重叠只增 13px
 *    - 若需要展示 "+N" 徽章，要再扣一个 13px 的位 */
function calcMaxAvatars(panelWidth: number, total: number): number {
  if (total <= 1) return total
  const reserved = 24 + 26 + 38 + 60 + 16
  const avail = Math.max(0, panelWidth - reserved)
  // 极窄情况下至少给 2 个位（避免只剩一个孤零零的头）
  const slots = Math.max(2, Math.floor((avail - 20) / 13) + 1)
  // 能装下全部就全显，不需要 +N
  if (slots >= total) return total
  // 需要 +N 徽章时，徽章本身也占一格 → 实际显示 slots - 1 个
  return Math.max(1, slots - 1)
}

/** 老 session 的 title 是 `场景：自由（柳如烟、风浪）` 这种包装格式；
 *  现在卡片下方已用色条 + tag 表达场景类型、用头像群表达参与者，
 *  标题再写一遍就纯冗余。剥掉外层只留参与者列表，跟新建 session 的 title 一致。
 *  匹配不上时（用户手改过）原样返回。 */
function stripLegacySceneTitle(title: string): string {
  const m = title.match(/^场景：[^（]+（(.+)）$/)
  return m ? m[1] : title
}

function CollabCard({
  item,
  agents,
  active,
  panelWidth,
  editing,
  editingTitle,
  onEditingTitleChange,
  onStartRename,
  onCommitRename,
  onCancelRename,
  onOpen,
  onDelete,
}: CollabCardProps) {
  const { session, sceneType, participants, isRunning } = item
  const sceneCfg = sceneType ? COLLAB_SCENE_CONFIGS[sceneType] : null
  const typeLabel = sceneCfg?.label || '协作'
  const typeColor = sceneCfg?.color || '#7fb8ff'
  const cleanTitle = stripLegacySceneTitle(session.title)
  const maxAvatars = calcMaxAvatars(panelWidth, participants.length)
  const hasMore = participants.length > maxAvatars
  const overflowCount = participants.length - maxAvatars

  // 方案 B 双行精致版：
  //   row 1: [标题省略 1 行] [live 圆点]
  //   row 2: [类型小标签] [叠头像群] [时间]
  // 信息没缩，但用紧凑布局把卡片高度从 ~78px 压到 ~52px，更像 Slack/Discord 的频道项。
  return (
    <div
      className={`roundtable-card ${active ? 'active' : ''} ${isRunning ? 'running' : ''}`}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen() } }}
      style={{ ['--card-accent' as string]: typeColor }}
    >
      <span className="roundtable-card-accent" aria-hidden="true" />

      <div className="roundtable-card-row1">
        {editing ? (
          <input
            className="roundtable-card-edit-input"
            value={editingTitle}
            onChange={(e) => onEditingTitleChange(e.target.value)}
            onClick={(e) => e.stopPropagation()}
            onBlur={onCommitRename}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); onCommitRename() }
              if (e.key === 'Escape') { e.preventDefault(); onCancelRename() }
            }}
            autoFocus
          />
        ) : (
          <span className="roundtable-card-title" title={cleanTitle}>
            {cleanTitle}
          </span>
        )}
        {isRunning && <span className="roundtable-card-live-dot" aria-label="进行中" />}
      </div>

      <div className="roundtable-card-row2">
        <span
          className="roundtable-card-tag"
          style={{ color: typeColor, borderColor: `${typeColor}55`, background: `${typeColor}12` }}
        >
          {typeLabel}
        </span>
        <div className="roundtable-card-avatars" aria-hidden="true">
          {participants.slice(0, maxAvatars).map((pid, idx) => {
            const agent = agents.find((a) => a.id === pid)
            return (
              <span
                key={pid}
                className="roundtable-card-avatar"
                style={{ zIndex: 10 - idx }}
                title={agent?.name || pid}
              >
                <AgentAvatar
                  name={agent?.name}
                  src={getAvatarSrc(agent || { avatar: undefined, avatar_url: undefined })}
                  imageClassName="roundtable-card-avatar-img"
                  plainClassName="roundtable-card-avatar-plain"
                />
              </span>
            )
          })}
          {hasMore && (
            <span className="roundtable-card-avatar roundtable-card-avatar-more">
              +{overflowCount}
            </span>
          )}
        </div>
        <span className="roundtable-card-time">{formatRelative(item.updatedAtMs)}</span>
      </div>

      {/* hover 显示的右上角操作组：编辑名称 + 删除 */}
      <div className="roundtable-card-actions" onClick={(e) => e.stopPropagation()}>
        <button
          className="roundtable-card-action"
          onClick={onStartRename}
          title="重命名"
          aria-label="重命名协作"
        >
          <Edit3 size={12} />
        </button>
        <button
          className="roundtable-card-action roundtable-card-action-danger"
          onClick={onDelete}
          title="删除"
          aria-label="删除协作"
        >
          <Trash2 size={12} />
        </button>
      </div>
    </div>
  )
}

function formatRelative(ms: number): string {
  if (!ms) return ''
  const diff = Date.now() - ms
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  const d = new Date(ms)
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')}`
}
