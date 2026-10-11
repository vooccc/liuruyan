import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Pin,
  Pencil,
  X,
  Plus,
  Trash2,
  Folder,
  FolderOpen,
  Globe,
  Server,
  ExternalLink,
  Copy,
  Check,
  ChevronDown,
  Terminal,
  RotateCcw,
  Lock,
} from 'lucide-react'
import {
  type ScopeItem,
  type ScopeItemType,
  type SessionPinnedState,
  getSessionPinned,
  updateSessionPinnedGuideline,
  updateSessionPinnedScope,
} from '../api/chat'
import { getLryStatus } from '../api/lry'
import { getRuntime } from '../runtime'
import { API_BASE } from '../api/base'

const SCOPE_TYPE_LABELS: Record<ScopeItemType, string> = {
  file: '文件',
  directory: '目录',
  code_context: 'Symbol',
  rule: '规则',
  code_map: 'Code Map',
}

const SCOPE_TYPES: ScopeItemType[] = ['file', 'directory', 'code_context', 'rule', 'code_map']

const RECENT_WORKSPACES_KEY = 'lry_recent_workspaces'

interface PinnedBarProps {
  sessionId: string | null
  /** 外部递增触发重新拉取（@ mention 选中后通知 PinnedBar 刷新）。 */
  refreshKey?: number
  /** 当前是否有任务在执行中（流式生成或工具调用执行期间锁定工作区切换，避免路径冲突） */
  isBusy?: boolean
  /** 当 sessionId 为空（新会话准备态）时，用户操作（切换工作区/编辑Pin）按需自动创建会话 */
  onEnsureSession?: () => Promise<string>
}

/* ============================================================================
   上拉式工作区列表菜单（支持一键打开本地文件夹、最近使用列表、SSH/WSL 远程连接）
   ============================================================================ */

interface WorkspacePopoverProps {
  currentPath: string
  defaultPath: string
  isCustom: boolean
  isRemote: boolean
  onSelectLocalPath: (path: string) => Promise<void>
  onSelectRemote: (url: string, label: string) => Promise<void>
  onResetDefault: () => Promise<void>
  onClose: () => void
  triggerRef?: React.RefObject<HTMLElement | null>
}

function WorkspacePopover({
  currentPath,
  defaultPath,
  isCustom,
  isRemote,
  onSelectLocalPath,
  onSelectRemote,
  onResetDefault,
  onClose,
  triggerRef,
}: WorkspacePopoverProps) {
  const popoverRef = useRef<HTMLDivElement>(null)
  const [tab, setTab] = useState<'local' | 'remote'>(isRemote ? 'remote' : 'local')
  const [remoteSubTab, setRemoteSubTab] = useState<'ssh' | 'wsl'>(
    currentPath.startsWith('\\\\wsl$') ? 'wsl' : 'ssh'
  )

  // SSH fields
  const [sshHost, setSshHost] = useState('')
  const [sshPort, setSshPort] = useState('22')
  const [sshUser, setSshUser] = useState('root')
  const [sshRemotePath, setSshRemotePath] = useState('/var/www/my-project')

  // WSL fields
  const [wslDistro, setWslDistro] = useState('Ubuntu')
  const [wslPath, setWslPath] = useState('/home/user/project')

  const [copied, setCopied] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [recentList, setRecentList] = useState<string[]>([])
  const [manualPath, setManualPath] = useState('')

  const handleApplyManualPath = async () => {
    const trimmed = manualPath.trim().replace(/^["']|["']$/g, '').trim()
    if (!trimmed) {
      setError('请输入项目绝对路径')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await onSelectLocalPath(trimmed)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '绑定项目路径失败')
    } finally {
      setBusy(false)
    }
  }

  // 点击外部与 ESC 键监听
  useEffect(() => {
    const handleMousedown = (e: MouseEvent) => {
      const target = e.target as Node
      if (
        popoverRef.current &&
        !popoverRef.current.contains(target) &&
        (!triggerRef?.current || !triggerRef.current.contains(target))
      ) {
        onClose()
      }
    }
    const handleKeydown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', handleMousedown)
    document.addEventListener('keydown', handleKeydown)
    return () => {
      document.removeEventListener('mousedown', handleMousedown)
      document.removeEventListener('keydown', handleKeydown)
    }
  }, [onClose, triggerRef])

  useEffect(() => {
    try {
      const saved = localStorage.getItem(RECENT_WORKSPACES_KEY)
      const list: string[] = saved ? JSON.parse(saved) : []
      if (defaultPath && !list.includes(defaultPath)) {
        list.push(defaultPath)
      }
      setRecentList(list)
    } catch {
      // ignore
    }
  }, [defaultPath])

  const handleCopyPath = () => {
    if (!currentPath) return
    navigator.clipboard.writeText(currentPath)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleOpenFolderInExplorer = async () => {
    if (!currentPath || isRemote) return
    try {
      await fetch(`${API_BASE}/workspace/open-folder`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: currentPath }),
      })
    } catch (err) {
      console.warn('打开文件夹失败:', err)
    }
  }

  // 一键呼出系统文件夹选择器（0 多余点击！）
  const handlePickLocalDirectory = async () => {
    setBusy(true)
    setError(null)
    try {
      const runtime = await getRuntime()
      const selected = await runtime.dialog.pickDirectory({
        title: '选择工作区项目文件夹',
        defaultPath: currentPath || defaultPath || undefined,
      })
      if (selected) {
        await onSelectLocalPath(selected)
        onClose()
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : '选择文件夹失败'
      if (msg.includes('dialog.pickDirectory') || msg.includes('当前运行环境不可用')) {
        setError('当前为浏览器模式，无法调起系统文件夹选择器。请在下方直接输入或粘贴项目绝对路径。')
      } else {
        setError(msg)
      }
    } finally {
      setBusy(false)
    }
  }

  const handleApplySsh = async () => {
    if (!sshHost.trim()) {
      setError('请输入远程服务器主机 IP 或域名')
      return
    }
    const host = sshHost.trim()
    const port = sshPort.trim() || '22'
    const user = sshUser.trim() || 'root'
    const rpath = sshRemotePath.trim() || '/'
    const sshUrl = `ssh://${user}@${host}:${port}${rpath.startsWith('/') ? rpath : `/${rpath}`}`
    const label = `SSH: ${host}${rpath}`
    setBusy(true)
    setError(null)
    try {
      await onSelectRemote(sshUrl, label)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '绑定远程工作区失败')
    } finally {
      setBusy(false)
    }
  }

  const handleApplyWsl = async () => {
    const distro = wslDistro.trim() || 'Ubuntu'
    const subPath = wslPath.trim().replace(/^\//, '').replace(/\//g, '\\')
    const fullWslPath = `\\\\wsl$\\${distro}\\${subPath}`
    setBusy(true)
    setError(null)
    try {
      await onSelectRemote(fullWslPath, `WSL: ${distro}`)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '绑定 WSL 工作区失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="workspace-popover" ref={popoverRef} role="menu">
      {/* 头部摘要与快捷工具 */}
      <div className="ws-popover-header">
        <div className="ws-popover-title">
          <span className={`pinned-workspace-dot ${isRemote ? 'is-remote' : 'is-local'}`} />
          <span className="ws-popover-title-text">
            {isRemote ? '远程连接' : isCustom ? '指定项目' : '默认根目录'}
          </span>
        </div>
        <div className="ws-popover-quick-tools">
          {currentPath && (
            <button
              type="button"
              className="ws-popover-tool-btn"
              onClick={handleCopyPath}
              title={copied ? '已复制！' : '复制完整路径'}
            >
              {copied ? <Check size={12} className="text-success" /> : <Copy size={12} />}
            </button>
          )}
          {!isRemote && currentPath && (
            <button
              type="button"
              className="ws-popover-tool-btn"
              onClick={handleOpenFolderInExplorer}
              title="在系统文件管理器中打开"
            >
              <ExternalLink size={12} />
            </button>
          )}
        </div>
      </div>

      {/* 当前路径预览卡片 */}
      <div className="ws-popover-path-bar" title={currentPath}>
        <span className="ws-popover-path-text">{currentPath || '(未设置工作区路径)'}</span>
      </div>

      {/* 选项卡：本地项目 vs 远程连接 */}
      <div className="ws-popover-tabs">
        <button
          type="button"
          className={`ws-popover-tab-btn ${tab === 'local' ? 'is-active' : ''}`}
          onClick={() => setTab('local')}
        >
          <Folder size={12} />
          <span>本地项目</span>
        </button>
        <button
          type="button"
          className={`ws-popover-tab-btn ${tab === 'remote' ? 'is-active' : ''}`}
          onClick={() => setTab('remote')}
        >
          <Globe size={12} />
          <span>远程连接</span>
        </button>
      </div>

      {/* 选项卡内容区 */}
      <div className="ws-popover-body">
        {tab === 'local' ? (
          <div className="ws-popover-local-pane">
            {/* 一键打开本地文件夹（直接呼出系统弹窗，0多余点击！） */}
            <button
              type="button"
              className="ws-popover-action-row"
              onClick={handlePickLocalDirectory}
              disabled={busy}
            >
              <div className="ws-popover-action-icon">
                <FolderOpen size={15} />
              </div>
              <div className="ws-popover-action-meta">
                <span className="ws-popover-action-main">打开本地文件夹…</span>
                <span className="ws-popover-action-sub">唤起系统资源管理器挑选项目</span>
              </div>
            </button>

            {/* 手动输入绝对路径（支持纯 Web 模式或快速粘贴路径） */}
            <div className="ws-popover-manual-block">
              <div className="ws-popover-section-label">手动指定项目路径</div>
              <div className="ws-popover-manual-row">
                <input
                  type="text"
                  className="ws-popover-manual-input"
                  placeholder="输入绝对路径，如 ."
                  value={manualPath}
                  onChange={(e) => setManualPath(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      handleApplyManualPath()
                    }
                  }}
                  disabled={busy}
                />
                <button
                  type="button"
                  className="ws-popover-manual-btn"
                  onClick={handleApplyManualPath}
                  disabled={busy || !manualPath.trim()}
                >
                  确定
                </button>
              </div>
            </div>

            {/* 最近使用项目列表 */}
            {recentList.length > 0 && (
              <div className="ws-popover-recent-block">
                <div className="ws-popover-section-label">最近使用</div>
                <div className="ws-popover-items-list">
                  {recentList.map((recentPath) => {
                    const isSelected =
                      recentPath.toLowerCase() === (currentPath || '').toLowerCase()
                    const leaf = recentPath.split(/[\\/]/).filter(Boolean).pop() || recentPath
                    const isDefault =
                      defaultPath && recentPath.toLowerCase() === defaultPath.toLowerCase()
                    return (
                      <button
                        key={recentPath}
                        type="button"
                        className={`ws-popover-item ${isSelected ? 'is-active' : ''}`}
                        onClick={async () => {
                          await onSelectLocalPath(recentPath)
                          onClose()
                        }}
                        title={recentPath}
                      >
                        <Folder size={13} className="ws-popover-item-icon" />
                        <div className="ws-popover-item-info">
                          <div className="ws-popover-item-name">
                            <span>{leaf}</span>
                            {isDefault && <span className="ws-item-tag">默认</span>}
                          </div>
                          <div className="ws-popover-item-path">{recentPath}</div>
                        </div>
                        {isSelected && <Check size={13} className="ws-popover-item-check" />}
                      </button>
                    )
                  })}
                </div>
              </div>
            )}

            {/* 恢复默认工程目录（仅在指定了自定义路径且非默认时显示） */}
            {isCustom && !isRemote && (
              <button
                type="button"
                className="ws-popover-reset-row"
                onClick={async () => {
                  await onResetDefault()
                  onClose()
                }}
                disabled={busy}
              >
                <RotateCcw size={12} />
                <span>恢复为工程默认根目录</span>
              </button>
            )}
          </div>
        ) : (
          <div className="ws-popover-remote-pane">
            <div className="ws-popover-remote-notice">
              💡 远程工作区当前处于实验性阶段，建议优先在本地检出代码以享受完整工具链支持。
            </div>

            {/* 子选项卡：SSH vs WSL */}
            <div className="ws-popover-subtabs">
              <button
                type="button"
                className={`ws-popover-subtab-btn ${remoteSubTab === 'ssh' ? 'is-active' : ''}`}
                onClick={() => setRemoteSubTab('ssh')}
              >
                <Server size={11} />
                <span>SSH 主机</span>
              </button>
              <button
                type="button"
                className={`ws-popover-subtab-btn ${remoteSubTab === 'wsl' ? 'is-active' : ''}`}
                onClick={() => setRemoteSubTab('wsl')}
              >
                <Terminal size={11} />
                <span>WSL 子系统</span>
              </button>
            </div>

            {remoteSubTab === 'ssh' ? (
              <div className="ws-popover-form">
                <div className="ws-form-grid-2">
                  <div className="ws-form-field">
                    <label>主机 / IP</label>
                    <input
                      type="text"
                      className="ws-form-input"
                      placeholder="192.168.1.100"
                      value={sshHost}
                      onChange={(e) => setSshHost(e.target.value)}
                    />
                  </div>
                  <div className="ws-form-field" style={{ width: '70px' }}>
                    <label>端口</label>
                    <input
                      type="text"
                      className="ws-form-input"
                      placeholder="22"
                      value={sshPort}
                      onChange={(e) => setSshPort(e.target.value)}
                    />
                  </div>
                </div>

                <div className="ws-form-field">
                  <label>用户名</label>
                  <input
                    type="text"
                    className="ws-form-input"
                    placeholder="root / ubuntu"
                    value={sshUser}
                    onChange={(e) => setSshUser(e.target.value)}
                  />
                </div>

                <div className="ws-form-field">
                  <label>远程项目目录</label>
                  <input
                    type="text"
                    className="ws-form-input"
                    placeholder="/var/www/my-project"
                    value={sshRemotePath}
                    onChange={(e) => setSshRemotePath(e.target.value)}
                  />
                </div>

                <div className="ws-remote-preview">
                  <code>
                    ssh://{sshUser || 'user'}@{sshHost || 'host'}:{sshPort || '22'}
                    {sshRemotePath.startsWith('/') ? sshRemotePath : `/${sshRemotePath}`}
                  </code>
                </div>

                <button
                  type="button"
                  className="ws-submit-btn"
                  onClick={handleApplySsh}
                  disabled={busy || !sshHost.trim()}
                >
                  连接并绑定远程工作区
                </button>
              </div>
            ) : (
              <div className="ws-popover-form">
                <div className="ws-form-field">
                  <label>WSL 发行版名称</label>
                  <div className="ws-distro-presets">
                    {['Ubuntu', 'Debian', 'openSUSE'].map((d) => (
                      <button
                        key={d}
                        type="button"
                        className={`ws-distro-pill ${wslDistro === d ? 'is-active' : ''}`}
                        onClick={() => setWslDistro(d)}
                      >
                        {d}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="ws-form-field">
                  <label>Linux 内部路径</label>
                  <input
                    type="text"
                    className="ws-form-input"
                    placeholder="/home/user/project"
                    value={wslPath}
                    onChange={(e) => setWslPath(e.target.value)}
                  />
                </div>

                <div className="ws-remote-preview">
                  <code>
                    \\wsl$\{wslDistro}\{wslPath.replace(/^\//, '').replace(/\//g, '\\')}
                  </code>
                </div>

                <button
                  type="button"
                  className="ws-submit-btn"
                  onClick={handleApplyWsl}
                  disabled={busy}
                >
                  绑定 WSL 工作区
                </button>
              </div>
            )}
          </div>
        )}

        {error && <div className="ws-popover-error">{error}</div>}
      </div>
    </div>
  )
}

/* ============================================================================
   上下文编辑器上拉浮层（风格完全对齐 WorkspacePopover）
   ============================================================================ */

interface PinnedEditorProps {
  initialGuideline: string | null
  initialScope: ScopeItem[]
  onSave: (guideline: string | null, scope: ScopeItem[]) => Promise<void>
  onClose: () => void
  triggerRef?: React.RefObject<HTMLElement | null>
}

function PinnedPopover({ initialGuideline, initialScope, onSave, onClose, triggerRef }: PinnedEditorProps) {
  const popoverRef = useRef<HTMLDivElement>(null)
  const [tab, setTab] = useState<'scope' | 'guideline'>('scope')
  const [guideline, setGuideline] = useState<string>(initialGuideline || '')
  const [scope, setScope] = useState<ScopeItem[]>(() => initialScope.map((it) => ({ ...it })))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 点击外部与 ESC 键监听
  useEffect(() => {
    const handleMousedown = (e: MouseEvent) => {
      const target = e.target as Node
      if (
        popoverRef.current &&
        !popoverRef.current.contains(target) &&
        (!triggerRef?.current || !triggerRef.current.contains(target))
      ) {
        onClose()
      }
    }
    const handleKeydown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', handleMousedown)
    document.addEventListener('keydown', handleKeydown)
    return () => {
      document.removeEventListener('mousedown', handleMousedown)
      document.removeEventListener('keydown', handleKeydown)
    }
  }, [onClose, triggerRef])

  const updateItem = useCallback((idx: number, patch: Partial<ScopeItem>) => {
    setScope((prev) => prev.map((it, i) => (i === idx ? { ...it, ...patch } : it)))
  }, [])

  const removeItem = useCallback((idx: number) => {
    setScope((prev) => prev.filter((_, i) => i !== idx))
  }, [])

  const addItem = useCallback(() => {
    setScope((prev) => [...prev, { type: 'file', path: '' }])
    setTab('scope')
  }, [])

  const handleClearAll = useCallback(() => {
    setGuideline('')
    setScope([])
  }, [])

  const handleSave = useCallback(async () => {
    setSaving(true)
    setError(null)
    try {
      const cleanedScope = scope
        .map((it) => ({ ...it, path: it.path.trim(), label: it.label?.trim() || undefined }))
        .filter((it) => it.type === 'code_map' || it.path.length > 0)
      const cleanedGuideline = guideline.trim() ? guideline : null
      await onSave(cleanedGuideline, cleanedScope)
      onClose()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }, [guideline, scope, onSave, onClose])

  const totalCount = scope.length + (guideline.trim() ? 1 : 0)

  return (
    <div className="pinned-popover" ref={popoverRef} role="menu">
      {/* 头部摘要与快捷工具 */}
      <div className="ws-popover-header">
        <div className="ws-popover-title">
          <span
            className="pinned-workspace-dot"
            style={
              totalCount > 0
                ? { background: '#6366f1', boxShadow: '0 0 5px rgba(99, 102, 241, 0.55)' }
                : { background: '#94a3b8' }
            }
          />
          <span className="ws-popover-title-text">Pinned Context</span>
          {totalCount > 0 && <span className="pinned-custom-badge">{totalCount} 项</span>}
        </div>
        <div className="ws-popover-quick-tools">
          {totalCount > 0 && (
            <button
              type="button"
              className="ws-popover-tool-btn"
              onClick={handleClearAll}
              title="清空所有 Pin 项与指令"
            >
              <Trash2 size={12} />
            </button>
          )}
          <button
            type="button"
            className="ws-popover-tool-btn"
            onClick={onClose}
            title="关闭"
          >
            <X size={12} />
          </button>
        </div>
      </div>

      {/* 当前 Context 概览卡片 */}
      <div className="ws-popover-path-bar" title={guideline || '未设置指令'}>
        <span className="ws-popover-path-text">
          {guideline.trim()
            ? `Guideline: ${guideline.trim()}`
            : scope.length > 0
            ? `已关注 ${scope.length} 项（${scope.map((s) => s.path || s.type).slice(0, 3).join(', ')}${scope.length > 3 ? '…' : ''}）`
            : '(未设置置顶规则或关注文件)'}
        </span>
      </div>

      {/* 选项卡：关注范围 vs 提示指令 */}
      <div className="ws-popover-tabs">
        <button
          type="button"
          className={`ws-popover-tab-btn ${tab === 'scope' ? 'is-active' : ''}`}
          onClick={() => setTab('scope')}
        >
          <Folder size={12} />
          <span>关注范围 ({scope.length})</span>
        </button>
        <button
          type="button"
          className={`ws-popover-tab-btn ${tab === 'guideline' ? 'is-active' : ''}`}
          onClick={() => setTab('guideline')}
        >
          <Pin size={12} />
          <span>提示指令 {guideline.trim() ? '(已设)' : ''}</span>
        </button>
      </div>

      {/* 选项卡内容区 */}
      <div className="ws-popover-body pinned-popover-body">
        {tab === 'scope' ? (
          <div className="pinned-popover-scope-pane">
            {/* 一键添加关注项（与打开本地文件夹风格一致的高质感卡片） */}
            <button
              type="button"
              className="ws-popover-action-row"
              onClick={addItem}
            >
              <div className="ws-popover-action-icon">
                <Plus size={15} />
              </div>
              <div className="ws-popover-action-meta">
                <span className="ws-popover-action-main">添加关注项…</span>
                <span className="ws-popover-action-sub">关注文件、目录、规则、Symbol 或 Code Map</span>
              </div>
            </button>

            {/* Scope 列表 */}
            {scope.length === 0 ? (
              <div className="pinned-empty-hint">还没有置顶任何文件或范围。点击上方「添加关注项」开始。</div>
            ) : (
              <div className="pinned-popover-items-list">
                {scope.map((item, idx) => (
                  <div key={idx} className="pinned-popover-scope-row">
                    <select
                      className="pinned-scope-type"
                      value={item.type}
                      onChange={(e) => updateItem(idx, { type: e.target.value as ScopeItemType })}
                    >
                      {SCOPE_TYPES.map((t) => (
                        <option key={t} value={t}>
                          {SCOPE_TYPE_LABELS[t]}
                        </option>
                      ))}
                    </select>
                    <input
                      className="pinned-scope-path"
                      type="text"
                      placeholder={
                        item.type === 'code_context'
                          ? 'symbol 名（如 SessionManager）'
                          : item.type === 'code_map'
                          ? '可留空，默认 CODE_MAP.md'
                          : '相对路径（如 backend/main.py）'
                      }
                      value={item.path}
                      onChange={(e) => updateItem(idx, { path: e.target.value })}
                    />
                    <input
                      className="pinned-scope-label"
                      type="text"
                      placeholder="显示名（可选）"
                      value={item.label || ''}
                      onChange={(e) => updateItem(idx, { label: e.target.value })}
                    />
                    <button
                      type="button"
                      className="pinned-icon-btn"
                      onClick={() => removeItem(idx)}
                      aria-label="删除"
                      title="删除此项"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="pinned-popover-guideline-pane">
            <div className="ws-popover-section-label">会话级 Guideline 指令</div>
            <div className="pinned-popover-guideline-hint">
              💡 每轮对话将自动注入到 System Prompt 末尾，用于指导 AI 回复原则与编码纪律。
            </div>
            <textarea
              className="pinned-guideline-input"
              value={guideline}
              onChange={(e) => setGuideline(e.target.value)}
              placeholder="例：始终用中文回复；优先改上游不要 workaround；禁止重构超出本次需求的代码"
              rows={5}
            />
          </div>
        )}

        {error && <div className="ws-popover-error">{error}</div>}
      </div>

      {/* 底部操作栏 */}
      <div className="pinned-popover-footer">
        <button
          type="button"
          className="pinned-btn-secondary pinned-popover-footer-btn"
          onClick={onClose}
          disabled={saving}
        >
          取消
        </button>
        <button
          type="button"
          className="pinned-btn-primary pinned-popover-footer-btn"
          onClick={handleSave}
          disabled={saving}
        >
          {saving ? '保存中…' : '保存变更'}
        </button>
      </div>
    </div>
  )
}

/* ============================================================================
   PinnedBar 主组件
   ============================================================================ */

export default function PinnedBar({ sessionId, refreshKey = 0, isBusy = false, onEnsureSession }: PinnedBarProps) {
  const [state, setState] = useState<SessionPinnedState | null>(null)
  const [editorOpen, setEditorOpen] = useState(false)
  const [workspacePopoverOpen, setWorkspacePopoverOpen] = useState(false)
  const [busyToast, setBusyToast] = useState(false)
  const [defaultWorkspaceRoot, setDefaultWorkspaceRoot] = useState<string>('')

  const workspaceTriggerRef = useRef<HTMLButtonElement>(null)
  const pinTriggerRef = useRef<HTMLButtonElement>(null)

  // 1. 初始化拉取后端默认工作区根目录
  useEffect(() => {
    getLryStatus()
      .then((st) => {
        if (st?.workspace_root) {
          setDefaultWorkspaceRoot(st.workspace_root)
        }
      })
      .catch(() => {
        // ignore
      })
  }, [])

  // 2. 刷新当前会话的 pinned 状态
  const refresh = useCallback(async (sid: string) => {
    try {
      const data = await getSessionPinned(sid)
      setState(data)
    } catch {
      setState(null)
    }
  }, [])

  useEffect(() => {
    if (!sessionId) {
      setState(null)
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const data = await getSessionPinned(sessionId)
        if (!cancelled) setState(data)
      } catch {
        if (!cancelled) setState(null)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [sessionId, refreshKey])

  // 识别当前会话是否指定了专属工作区
  const workspaceScopeIdx =
    state?.scope?.findIndex(
      (it) =>
        it.type === 'directory' &&
        (it.label?.includes('工作区') ||
          it.label?.includes('远程') ||
          it.label?.includes('WSL') ||
          it.label?.includes('SSH') ||
          it.path.startsWith('ssh://') ||
          it.path.startsWith('\\\\wsl$'))
    ) ?? -1

  const activeWorkspaceItem = workspaceScopeIdx >= 0 ? state!.scope[workspaceScopeIdx] : null
  const isCustomWorkspace = !!activeWorkspaceItem
  const currentWorkspacePath = activeWorkspaceItem ? activeWorkspaceItem.path : defaultWorkspaceRoot

  // 远程判定
  const isRemoteWorkspace =
    currentWorkspacePath.startsWith('ssh://') ||
    currentWorkspacePath.startsWith('\\\\wsl$') ||
    currentWorkspacePath.includes('@')

  const remoteKind = currentWorkspacePath.startsWith('\\\\wsl$') ? 'WSL' : 'SSH'

  // 短名称提取
  let workspaceShortName = '指定工作区'
  if (currentWorkspacePath) {
    if (isRemoteWorkspace) {
      if (currentWorkspacePath.startsWith('ssh://')) {
        const afterProtocol = currentWorkspacePath.replace(/^ssh:\/\//, '')
        const hostPart = afterProtocol.split('/')[0] || ''
        const lastLeaf = afterProtocol.split('/').filter(Boolean).pop() || ''
        workspaceShortName = `${hostPart}:${lastLeaf}`
      } else {
        workspaceShortName = currentWorkspacePath.split(/[\\/]/).filter(Boolean).pop() || '远程'
      }
    } else {
      workspaceShortName = currentWorkspacePath.split(/[\\/]/).filter(Boolean).pop() || currentWorkspacePath
    }
  }

  // 切换工作区逻辑
  const handleSelectLocalWorkspace = useCallback(
    async (newPath: string) => {
      let targetSid = sessionId
      if (!targetSid) {
        if (!onEnsureSession) return
        targetSid = await onEnsureSession()
        if (!targetSid) return
      }
      const short = newPath.split(/[\\/]/).filter(Boolean).pop() || newPath
      const filteredScope = (state?.scope ?? []).filter((_, idx) => idx !== workspaceScopeIdx)
      const newScopeItem: ScopeItem = {
        type: 'directory',
        path: newPath,
        label: `工作区: ${short}`,
      }
      const nextScope = [newScopeItem, ...filteredScope]

      // 记入 localStorage
      try {
        const raw = localStorage.getItem(RECENT_WORKSPACES_KEY)
        const recents: string[] = raw ? JSON.parse(raw) : []
        const updated = [newPath, ...recents.filter((p) => p !== newPath)].slice(0, 6)
        localStorage.setItem(RECENT_WORKSPACES_KEY, JSON.stringify(updated))
      } catch {
        // ignore
      }

      const result = await updateSessionPinnedScope(targetSid, nextScope)
      setState((prev) => (prev ? { ...prev, session_id: targetSid!, scope: result.scope } : { session_id: targetSid!, guideline: null, scope: result.scope }))
    },
    [sessionId, state, workspaceScopeIdx, onEnsureSession]
  )

  const handleSelectRemoteWorkspace = useCallback(
    async (remoteUrl: string, label: string) => {
      let targetSid = sessionId
      if (!targetSid) {
        if (!onEnsureSession) return
        targetSid = await onEnsureSession()
        if (!targetSid) return
      }
      const filteredScope = (state?.scope ?? []).filter((_, idx) => idx !== workspaceScopeIdx)
      const newScopeItem: ScopeItem = {
        type: 'directory',
        path: remoteUrl,
        label: label,
      }
      const nextScope = [newScopeItem, ...filteredScope]

      const result = await updateSessionPinnedScope(targetSid, nextScope)
      setState((prev) => (prev ? { ...prev, session_id: targetSid!, scope: result.scope } : { session_id: targetSid!, guideline: null, scope: result.scope }))
    },
    [sessionId, state, workspaceScopeIdx, onEnsureSession]
  )

  const handleResetDefaultWorkspace = useCallback(async () => {
    if (!sessionId || !state || workspaceScopeIdx < 0) return
    const nextScope = state.scope.filter((_, idx) => idx !== workspaceScopeIdx)
    const result = await updateSessionPinnedScope(sessionId, nextScope)
    setState((prev) => (prev ? { ...prev, scope: result.scope } : prev))
  }, [sessionId, state, workspaceScopeIdx])

  const handleRemoveItem = useCallback(
    async (idx: number) => {
      if (!sessionId || !state) return
      const next = state.scope.filter((_, i) => i !== idx)
      try {
        const result = await updateSessionPinnedScope(sessionId, next)
        setState((prev) => (prev ? { ...prev, scope: result.scope } : prev))
      } catch {
        void refresh(sessionId)
      }
    },
    [sessionId, state, refresh]
  )

  const handleSaveEditor = useCallback(
    async (guideline: string | null, scope: ScopeItem[]) => {
      let targetSid = sessionId
      if (!targetSid) {
        if (!onEnsureSession) return
        targetSid = await onEnsureSession()
        if (!targetSid) return
      }
      await updateSessionPinnedGuideline(targetSid, guideline)
      const result = await updateSessionPinnedScope(targetSid, scope)
      setState({
        session_id: targetSid,
        guideline,
        scope: result.scope,
      })
    },
    [sessionId, onEnsureSession]
  )

  if (!sessionId && !onEnsureSession) return null

  const hasGuideline = !!state?.guideline?.trim()
  // 过滤掉已经在最左侧工作区胶囊展示的工作区项，防止中间重复渲染
  const otherScopeItems = (state?.scope ?? []).filter((_, idx) => idx !== workspaceScopeIdx)
  const hasOtherScope = otherScopeItems.length > 0
  const hasAnyContext = hasGuideline || hasOtherScope

  return (
    <>
      <div className={`pinned-bar ${hasAnyContext || currentWorkspacePath ? '' : 'pinned-bar--empty'}`}>
        {/* 最左侧：专属工作区胶囊 + 上拉式浮层菜单 */}
        <div className="pinned-workspace-wrapper">
          <button
            ref={workspaceTriggerRef}
            type="button"
            className={`pinned-workspace-pill ${isRemoteWorkspace ? 'is-remote' : ''} ${
              isCustomWorkspace ? 'is-custom' : ''
            } ${isBusy ? 'is-busy' : ''} ${workspacePopoverOpen ? 'is-open' : ''}`}
            onClick={() => {
              if (isBusy) {
                setBusyToast(true)
                setTimeout(() => setBusyToast(false), 2400)
                return
              }
              setWorkspacePopoverOpen(!workspacePopoverOpen)
            }}
            title={
              isBusy
                ? '任务正在执行中，已锁定当前工作区以防文件读写冲突'
                : `当前工作区: ${currentWorkspacePath || '未指定'}\n点击快速切换项目或连接远程`
            }
          >
            <span className="pinned-ws-status-slot">
              {isBusy ? (
                <Lock size={12} className="pinned-ws-lock-icon" />
              ) : (
                <span className={`pinned-workspace-dot ${isRemoteWorkspace ? 'is-remote' : 'is-local'}`} />
              )}
            </span>
            {isRemoteWorkspace ? (
              <Globe size={13} className="pinned-ws-icon" />
            ) : (
              <Folder size={13} className="pinned-ws-icon" />
            )}
            <span className="pinned-ws-label">{workspaceShortName}</span>
            {isRemoteWorkspace && <span className="pinned-remote-badge">{remoteKind}</span>}
            {isCustomWorkspace && !isRemoteWorkspace && <span className="pinned-custom-badge">指定</span>}
            <ChevronDown
              size={11}
              className={`pinned-ws-arrow ${workspacePopoverOpen ? 'is-open' : ''}`}
            />
          </button>

          {/* 任务执行中锁定提示浮层 */}
          {busyToast && (
            <div className="pinned-busy-toast">
              <Lock size={12} />
              <span>任务执行中，已锁定工作区以防冲突</span>
            </div>
          )}

          {/* 上拉式工作区切换浮层 */}
          {workspacePopoverOpen && (
            <WorkspacePopover
              currentPath={currentWorkspacePath}
              defaultPath={defaultWorkspaceRoot}
              isCustom={isCustomWorkspace}
              isRemote={isRemoteWorkspace}
              onSelectLocalPath={handleSelectLocalWorkspace}
              onSelectRemote={handleSelectRemoteWorkspace}
              onResetDefault={handleResetDefaultWorkspace}
              onClose={() => setWorkspacePopoverOpen(false)}
              triggerRef={workspaceTriggerRef}
            />
          )}
        </div>

        {/* Guideline Chip */}
        {hasGuideline && (
          <div className="pinned-guideline-chip" title={state!.guideline || ''}>
            <Pin size={12} />
            <span className="pinned-guideline-chip-text">
              {(state!.guideline || '').length > 50
                ? `${(state!.guideline || '').slice(0, 50)}…`
                : state!.guideline}
            </span>
          </div>
        )}

        {/* 其它已 Pin 的文件 / 规则 / Code Map */}
        {hasOtherScope &&
          otherScopeItems.map((item, originalIdx) => {
            const actualIdx = state!.scope.indexOf(item)
            const label =
              item.label ||
              (item.type === 'code_context'
                ? item.path
                : item.path.split(/[\\/]/).pop() || item.path)
            return (
              <div
                key={`${item.type}:${item.path}:${originalIdx}`}
                className="pinned-scope-chip"
                title={`${SCOPE_TYPE_LABELS[item.type]}: ${item.path}`}
              >
                <span className="pinned-scope-chip-type">{SCOPE_TYPE_LABELS[item.type]}</span>
                <span className="pinned-scope-chip-label">{label || '(default)'}</span>
                <button
                  type="button"
                  className="pinned-scope-chip-remove"
                  onClick={() => handleRemoveItem(actualIdx)}
                  aria-label={`移除 ${label}`}
                >
                  <X size={11} />
                </button>
              </div>
            )
          })}

        {/* Pin 上下文编辑按钮与上拉浮层 */}
        <div className="pinned-popover-wrapper">
          <button
            ref={pinTriggerRef}
            type="button"
            className={`pinned-edit-btn ${editorOpen ? 'is-open' : ''} ${hasAnyContext ? 'has-context' : ''}`}
            onClick={() => setEditorOpen(!editorOpen)}
            title="编辑 Pinned Context（Guideline 与关注文件）"
          >
            {hasAnyContext ? (
              <>
                <Pencil size={12} />
                <span>Pin</span>
              </>
            ) : (
              <>
                <Pin size={12} />
                <span>Pin</span>
              </>
            )}
            <ChevronDown
              size={11}
              className={`pinned-edit-arrow ${editorOpen ? 'is-open' : ''}`}
            />
          </button>

          {/* 规则与 Scope 上拉浮层 */}
          {editorOpen && (
            <PinnedPopover
              initialGuideline={state?.guideline ?? null}
              initialScope={state?.scope ?? []}
              onSave={handleSaveEditor}
              onClose={() => setEditorOpen(false)}
              triggerRef={pinTriggerRef}
            />
          )}
        </div>
      </div>

      <PinnedStyle />
    </>
  )
}

/* ============================================================================
   组件样式（高质感自适配、暗黑兼容）
   ============================================================================ */

function PinnedStyle() {
  return (
    <style>{`
      /* 浮动容器：无沉重灰条，轻盈融入聊天输入框顶部，紧贴输入框精准对齐 */
      .pinned-bar {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
        padding: 0;
        margin-bottom: 5px;
        background: transparent !important;
        border: none !important;
        font-size: 12px;
      }
      .pinned-bar--empty {
        display: flex;
      }

      /* 工作区包裹层（定位基准） */
      .pinned-workspace-wrapper {
        position: relative;
        display: inline-flex;
        align-items: center;
      }

      /* 弧形胶囊工作区按钮（简约大方原生质感） */
      .pinned-workspace-pill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        height: 28px;
        padding: 0 10px 0 9px;
        border-radius: 9999px;
        background: var(--bg-card, #ffffff);
        border: 1px solid var(--border-light, rgba(0, 0, 0, 0.1));
        color: var(--text-primary, #334155);
        font-weight: 500;
        cursor: pointer;
        transition: all 0.18s cubic-bezier(0.16, 1, 0.3, 1);
        max-width: 260px;
        user-select: none;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
      }
      .pinned-workspace-pill:hover {
        background: #ffffff;
        border-color: rgba(0, 0, 0, 0.2);
        box-shadow: 0 2px 6px rgba(0, 0, 0, 0.06);
        color: var(--text-primary, #0f172a);
      }
      .pinned-workspace-pill.is-open {
        border-color: var(--accent, #6366f1);
        box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.16);
      }
      .pinned-workspace-pill.is-busy {
        cursor: not-allowed;
        border-color: rgba(245, 158, 11, 0.35);
        background: rgba(245, 158, 11, 0.05);
      }
      /* 状态槽位：固定 14px 宽度，确保闲置点与锁定锁切换时按钮大小恒定不变 */
      .pinned-ws-status-slot {
        width: 14px;
        height: 14px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
      }
      .pinned-ws-lock-icon {
        color: #f59e0b;
        flex-shrink: 0;
      }
      .pinned-workspace-pill.is-remote {
        border-color: rgba(14, 165, 233, 0.3);
      }
      .pinned-workspace-pill.is-remote:hover {
        border-color: rgba(14, 165, 233, 0.5);
      }

      /* 任务执行中锁定提示浮层 */
      .pinned-busy-toast {
        position: absolute;
        bottom: calc(100% + 8px);
        left: 0;
        z-index: 1001;
        background: var(--bg-card, #1c1c20);
        color: #f59e0b;
        border: 1px solid rgba(245, 158, 11, 0.4);
        border-radius: 8px;
        padding: 7px 12px;
        font-size: 11.5px;
        display: flex;
        align-items: center;
        gap: 6px;
        white-space: nowrap;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
        animation: wsPopoverSlideUp 0.15s cubic-bezier(0.16, 1, 0.3, 1);
      }

      /* 在线指示小圆点 */
      .pinned-workspace-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        flex-shrink: 0;
      }
      .pinned-workspace-dot.is-local {
        background: #10b981;
        box-shadow: 0 0 4px rgba(16, 185, 129, 0.5);
      }
      .pinned-workspace-dot.is-remote {
        background: #0ea5e9;
        box-shadow: 0 0 4px rgba(14, 165, 233, 0.5);
      }

      .pinned-ws-icon {
        color: #64748b;
        flex-shrink: 0;
      }
      .pinned-ws-label {
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        font-size: 12px;
        font-family: inherit;
        letter-spacing: -0.01em;
      }
      .pinned-ws-arrow {
        color: #94a3b8;
        transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        flex-shrink: 0;
      }
      .pinned-ws-arrow.is-open {
        transform: rotate(180deg);
      }

      .pinned-remote-badge,
      .pinned-custom-badge {
        font-size: 9px;
        line-height: 1;
        padding: 2px 5px;
        border-radius: 9999px;
        font-weight: 600;
        letter-spacing: 0.2px;
      }
      .pinned-remote-badge {
        background: rgba(14, 165, 233, 0.12);
        color: #0284c7;
      }
      .pinned-custom-badge {
        background: rgba(99, 102, 241, 0.1);
        color: #6366f1;
      }

      /* Pin 编辑按钮外层包裹（定位基准，与工作区胶囊对齐） */
      .pinned-popover-wrapper {
        position: relative;
        display: inline-flex;
        align-items: center;
      }

      /* 弧形 Pin 按钮（与工作区胶囊对齐） */
      .pinned-edit-btn {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        height: 28px;
        padding: 0 10px;
        border-radius: 9999px;
        border: 1px solid var(--border-light, rgba(0, 0, 0, 0.08));
        background: var(--bg-card, rgba(255, 255, 255, 0.8));
        color: var(--text-secondary, #64748b);
        cursor: pointer;
        font-size: 11.5px;
        font-weight: 500;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
        transition: all 0.15s ease;
      }
      .pinned-edit-btn:hover {
        background: #ffffff;
        color: var(--text-primary, #1e293b);
        border-color: rgba(0, 0, 0, 0.18);
        box-shadow: 0 2px 5px rgba(0, 0, 0, 0.05);
      }
      .pinned-edit-btn.is-open {
        border-color: var(--accent, #6366f1);
        box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.16);
        color: var(--accent, #6366f1);
        background: #ffffff;
      }
      .pinned-edit-btn.has-context {
        color: var(--accent, #6366f1);
        background: rgba(99, 102, 241, 0.06);
        border-color: rgba(99, 102, 241, 0.22);
      }
      .pinned-edit-arrow {
        color: #94a3b8;
        transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        flex-shrink: 0;
      }
      .pinned-edit-arrow.is-open {
        transform: rotate(180deg);
      }

      /* 既有 Guideline 与 Scope Chips 弧形化 */
      .pinned-guideline-chip,
      .pinned-scope-chip {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        height: 28px;
        padding: 0 10px;
        border-radius: 9999px;
        background: var(--bg-card, #ffffff);
        color: var(--text-primary, #334155);
        max-width: 280px;
        line-height: 1;
        border: 1px solid var(--border-light, rgba(0, 0, 0, 0.08));
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
        font-size: 11.5px;
      }
      .pinned-guideline-chip {
        background: rgba(99, 102, 241, 0.06);
        border-color: rgba(99, 102, 241, 0.2);
        color: #4f46e5;
      }
      .pinned-guideline-chip-text {
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        font-weight: 500;
      }
      .pinned-scope-chip-type {
        font-size: 10px;
        opacity: 0.65;
        font-weight: 600;
        text-transform: uppercase;
      }
      .pinned-scope-chip-label {
        font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
        font-size: 11px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 170px;
      }
      .pinned-scope-chip-remove {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 14px;
        height: 14px;
        padding: 0;
        margin-left: 2px;
        border: none;
        background: transparent;
        cursor: pointer;
        opacity: 0.45;
        border-radius: 50%;
        color: inherit;
        transition: all 0.12s;
      }
      .pinned-scope-chip-remove:hover {
        opacity: 1;
        background: rgba(0, 0, 0, 0.08);
      }

      /* ======================================================================
         Pin 上拉浮层（Pinned Popover，与 Workspace Popover 视觉系统完全一致）
         ====================================================================== */
      .pinned-popover {
        position: absolute;
        bottom: calc(100% + 8px);
        left: 0;
        width: 380px;
        max-width: 90vw;
        background: var(--bg-card, #ffffff);
        color: var(--text-primary, #222);
        border: 1px solid var(--border-light, rgba(0, 0, 0, 0.12));
        border-radius: 12px;
        box-shadow: 0 16px 40px rgba(0, 0, 0, 0.28), 0 2px 8px rgba(0, 0, 0, 0.12);
        z-index: 1000;
        display: flex;
        flex-direction: column;
        overflow: hidden;
        animation: wsPopoverSlideUp 0.18s cubic-bezier(0.16, 1, 0.3, 1);
      }
      .pinned-popover-body {
        padding: 10px 12px 12px;
        max-height: 380px;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .pinned-popover-scope-pane,
      .pinned-popover-guideline-pane {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .pinned-popover-guideline-hint {
        font-size: 11px;
        line-height: 1.4;
        color: var(--text-secondary, #666);
        background: rgba(99, 102, 241, 0.06);
        border: 1px solid rgba(99, 102, 241, 0.16);
        border-radius: 6px;
        padding: 6px 8px;
      }
      .pinned-popover-items-list {
        display: flex;
        flex-direction: column;
        gap: 6px;
        max-height: 220px;
        overflow-y: auto;
        padding-right: 2px;
      }
      .pinned-popover-scope-row {
        display: grid;
        grid-template-columns: 86px 1fr 100px 24px;
        gap: 6px;
        align-items: center;
        padding: 4px 6px;
        border-radius: 6px;
        border: 1px solid var(--border-light, rgba(0, 0, 0, 0.08));
        background: var(--bg-card, #ffffff);
        transition: all 0.12s;
      }
      .pinned-popover-scope-row:hover {
        border-color: rgba(99, 102, 241, 0.25);
        background: var(--bg-elevated, rgba(0, 0, 0, 0.02));
      }
      .pinned-popover-scope-row .pinned-scope-type {
        font-size: 11px;
        padding: 4px 6px;
        border-radius: 4px;
        border: 1px solid var(--border-light, rgba(0, 0, 0, 0.12));
        background: var(--bg-card, #fff);
        color: inherit;
      }
      .pinned-popover-scope-row .pinned-scope-path {
        font-size: 11px;
        padding: 4px 6px;
        border-radius: 4px;
        border: 1px solid var(--border-light, rgba(0, 0, 0, 0.12));
        background: var(--bg-card, #fff);
        color: inherit;
        font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      }
      .pinned-popover-scope-row .pinned-scope-label {
        font-size: 11px;
        padding: 4px 6px;
        border-radius: 4px;
        border: 1px solid var(--border-light, rgba(0, 0, 0, 0.12));
        background: var(--bg-card, #fff);
        color: inherit;
      }
      .pinned-popover-scope-row input:focus,
      .pinned-popover-scope-row select:focus {
        outline: none;
        border-color: var(--accent, #6366f1);
      }
      .pinned-popover-footer {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 8px;
        padding: 8px 12px;
        border-top: 1px solid var(--border-light, rgba(0, 0, 0, 0.08));
        background: var(--bg-card, #ffffff);
      }
      .pinned-popover-footer-btn {
        flex: 1;
        padding: 6px 12px;
        font-size: 12px;
        text-align: center;
      }

      /* ======================================================================
         上拉式工作区浮层（Workspace Popover）
         ====================================================================== */
      .workspace-popover {
        position: absolute;
        bottom: calc(100% + 8px);
        left: 0;
        width: 350px;
        max-width: 90vw;
        background: var(--bg-card, #ffffff);
        color: var(--text-primary, #222);
        border: 1px solid var(--border-light, rgba(0, 0, 0, 0.12));
        border-radius: 12px;
        box-shadow: 0 16px 40px rgba(0, 0, 0, 0.28), 0 2px 8px rgba(0, 0, 0, 0.12);
        z-index: 1000;
        display: flex;
        flex-direction: column;
        overflow: hidden;
        animation: wsPopoverSlideUp 0.18s cubic-bezier(0.16, 1, 0.3, 1);
      }
      @keyframes wsPopoverSlideUp {
        from {
          opacity: 0;
          transform: translateY(6px) scale(0.97);
        }
        to {
          opacity: 1;
          transform: translateY(0) scale(1);
        }
      }

      .ws-popover-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 14px 6px;
      }
      .ws-popover-title {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        font-weight: 600;
      }
      .ws-popover-quick-tools {
        display: flex;
        align-items: center;
        gap: 4px;
      }
      .ws-popover-tool-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 4px 6px;
        border-radius: 4px;
        border: none;
        background: transparent;
        color: var(--text-secondary, #666);
        cursor: pointer;
        transition: all 0.12s;
      }
      .ws-popover-tool-btn:hover {
        background: var(--bg-elevated, rgba(0,0,0,0.06));
        color: var(--text-primary, #111);
      }

      .ws-popover-path-bar {
        margin: 0 12px 8px;
        padding: 6px 9px;
        border-radius: 6px;
        background: var(--bg-elevated, rgba(0,0,0,0.03));
        border: 1px solid var(--border-light, rgba(0,0,0,0.06));
        font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
        font-size: 11px;
        color: var(--text-secondary, #555);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .ws-popover-tabs {
        display: flex;
        border-bottom: 1px solid var(--border-light, rgba(0,0,0,0.08));
        padding: 0 12px;
        gap: 4px;
      }
      .ws-popover-tab-btn {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 6px 10px;
        background: transparent;
        border: none;
        border-bottom: 2px solid transparent;
        margin-bottom: -1px;
        color: var(--text-secondary, #666);
        cursor: pointer;
        font-size: 12px;
        font-weight: 500;
        transition: all 0.12s;
      }
      .ws-popover-tab-btn:hover {
        color: var(--text-primary, #222);
      }
      .ws-popover-tab-btn.is-active {
        color: var(--accent, #6366f1);
        border-bottom-color: var(--accent, #6366f1);
      }

      .ws-popover-body {
        padding: 10px 12px 12px;
        max-height: 360px;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      /* 本地选项卡 */
      .ws-popover-local-pane {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .ws-popover-action-row {
        display: flex;
        align-items: center;
        gap: 10px;
        width: 100%;
        padding: 8px 10px;
        border-radius: 8px;
        border: 1px dashed var(--accent, #6366f1);
        background: rgba(99, 102, 241, 0.05);
        color: var(--accent, #6366f1);
        cursor: pointer;
        text-align: left;
        transition: all 0.15s;
      }
      .ws-popover-action-row:hover {
        background: rgba(99, 102, 241, 0.12);
        border-style: solid;
      }
      .ws-popover-action-icon {
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
      }
      .ws-popover-action-meta {
        display: flex;
        flex-direction: column;
        gap: 1px;
      }
      .ws-popover-action-main {
        font-size: 12.5px;
        font-weight: 600;
        line-height: 1.2;
      }
      .ws-popover-action-sub {
        font-size: 11px;
        color: var(--text-secondary, #777);
        font-weight: 400;
      }

      .ws-popover-recent-block {
        display: flex;
        flex-direction: column;
        gap: 4px;
        margin-top: 4px;
      }
      .ws-popover-section-label {
        font-size: 11px;
        font-weight: 600;
        color: var(--text-secondary, #777);
        padding: 0 4px;
        text-transform: uppercase;
        letter-spacing: 0.3px;
      }
      .ws-popover-items-list {
        display: flex;
        flex-direction: column;
        gap: 2px;
        max-height: 180px;
        overflow-y: auto;
      }
      .ws-popover-item {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 8px;
        border-radius: 6px;
        border: 1px solid transparent;
        background: transparent;
        color: inherit;
        cursor: pointer;
        text-align: left;
        width: 100%;
        transition: all 0.12s;
      }
      .ws-popover-item:hover {
        background: var(--bg-elevated, rgba(0,0,0,0.04));
        border-color: var(--border-light, rgba(0,0,0,0.06));
      }
      .ws-popover-item.is-active {
        background: rgba(99, 102, 241, 0.08);
        border-color: rgba(99, 102, 241, 0.25);
      }
      .ws-popover-item-icon {
        color: var(--text-secondary, #888);
        flex-shrink: 0;
      }
      .ws-popover-item.is-active .ws-popover-item-icon {
        color: var(--accent, #6366f1);
      }
      .ws-popover-item-info {
        flex: 1;
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 1px;
      }
      .ws-popover-item-name {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        font-weight: 500;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .ws-item-tag {
        font-size: 9px;
        background: rgba(16, 185, 129, 0.12);
        color: #10b981;
        padding: 1px 4px;
        border-radius: 3px;
        font-weight: 600;
      }
      .ws-popover-item-path {
        font-size: 10.5px;
        color: var(--text-secondary, #888);
        font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .ws-popover-item-check {
        color: #10b981;
        flex-shrink: 0;
      }
      .ws-popover-reset-row {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 6px 8px;
        background: transparent;
        border: none;
        color: var(--text-secondary, #777);
        font-size: 11.5px;
        cursor: pointer;
        border-radius: 4px;
        margin-top: 2px;
      }
      .ws-popover-reset-row:hover {
        color: var(--text-primary, #333);
        background: var(--bg-elevated, rgba(0,0,0,0.04));
      }

      /* 远程选项卡 */
      .ws-popover-remote-pane {
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .ws-popover-subtabs {
        display: flex;
        gap: 6px;
      }
      .ws-popover-subtab-btn {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 3px 8px;
        border-radius: 4px;
        border: 1px solid var(--border-light, rgba(0,0,0,0.1));
        background: transparent;
        color: var(--text-secondary, #666);
        cursor: pointer;
        font-size: 11px;
      }
      .ws-popover-subtab-btn.is-active {
        border-color: var(--accent, #6366f1);
        background: rgba(99, 102, 241, 0.08);
        color: var(--accent, #6366f1);
        font-weight: 500;
      }
      .ws-popover-form {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .ws-form-grid-2 {
        display: flex;
        gap: 6px;
      }
      .ws-form-field {
        display: flex;
        flex-direction: column;
        gap: 3px;
        flex: 1;
      }
      .ws-form-field label {
        font-size: 10.5px;
        color: var(--text-secondary, #777);
        font-weight: 500;
      }
      .ws-form-input {
        padding: 5px 8px;
        border-radius: 5px;
        border: 1px solid var(--border-light, rgba(0,0,0,0.12));
        background: var(--bg-card, #fff);
        color: inherit;
        font-size: 11.5px;
        font-family: inherit;
      }
      .ws-form-input:focus {
        outline: none;
        border-color: var(--accent, #6366f1);
      }
      .ws-remote-preview {
        padding: 6px 8px;
        border-radius: 5px;
        background: var(--bg-elevated, rgba(0,0,0,0.03));
        border: 1px solid var(--border-light, rgba(0,0,0,0.06));
        font-size: 10.5px;
        word-break: break-all;
      }
      .ws-remote-preview code {
        color: #0284c7;
        font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      }
      .ws-distro-presets {
        display: flex;
        gap: 4px;
      }
      .ws-distro-pill {
        padding: 2px 6px;
        border-radius: 4px;
        border: 1px solid var(--border-light, rgba(0,0,0,0.1));
        background: transparent;
        color: var(--text-secondary, #666);
        cursor: pointer;
        font-size: 10.5px;
      }
      .ws-distro-pill.is-active {
        border-color: var(--accent, #6366f1);
        background: rgba(99, 102, 241, 0.1);
        color: var(--accent, #6366f1);
        font-weight: 500;
      }
      .ws-submit-btn {
        width: 100%;
        padding: 6px;
        border-radius: 6px;
        background: var(--accent, #6366f1);
        color: #fff;
        border: none;
        cursor: pointer;
        font-size: 12px;
        font-weight: 500;
        margin-top: 2px;
      }
      .ws-submit-btn:disabled {
        opacity: 0.55;
        cursor: not-allowed;
      }
      .ws-popover-error {
        color: #ef4444;
        font-size: 11px;
        padding-top: 2px;
      }
      .ws-popover-manual-block {
        display: flex;
        flex-direction: column;
        gap: 5px;
        margin-top: 4px;
        padding-top: 6px;
        border-top: 1px solid var(--border-light, rgba(0,0,0,0.06));
      }
      .ws-popover-manual-row {
        display: flex;
        gap: 6px;
        align-items: center;
      }
      .ws-popover-manual-input {
        flex: 1;
        padding: 5px 8px;
        border-radius: 6px;
        border: 1px solid var(--border-light, rgba(0,0,0,0.12));
        background: var(--bg-card, #fff);
        color: inherit;
        font-size: 11.5px;
        font-family: inherit;
      }
      .ws-popover-manual-input:focus {
        outline: none;
        border-color: var(--accent, #6366f1);
      }
      .ws-popover-manual-btn {
        padding: 5px 10px;
        border-radius: 6px;
        background: var(--accent, #6366f1);
        color: #fff;
        border: none;
        cursor: pointer;
        font-size: 11.5px;
        font-weight: 500;
        white-space: nowrap;
        transition: opacity 0.15s;
      }
      .ws-popover-manual-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .ws-popover-remote-notice {
        font-size: 11px;
        line-height: 1.4;
        color: var(--text-secondary, #666);
        background: rgba(245, 158, 11, 0.08);
        border: 1px solid rgba(245, 158, 11, 0.2);
        border-radius: 6px;
        padding: 6px 8px;
        margin-bottom: 2px;
      }

      /* 暗色模式与跨主题适配 */
      body.dark .pinned-workspace-pill,
      [data-theme="dark"] .pinned-workspace-pill,
      .dark .pinned-workspace-pill {
        background: #18181b;
        border-color: rgba(255, 255, 255, 0.12);
        color: #f4f4f5;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
      }
      body.dark .pinned-workspace-pill:hover,
      [data-theme="dark"] .pinned-workspace-pill:hover,
      .dark .pinned-workspace-pill:hover {
        background: #27272a;
        border-color: rgba(255, 255, 255, 0.22);
        color: #ffffff;
      }
      body.dark .pinned-edit-btn,
      [data-theme="dark"] .pinned-edit-btn,
      .dark .pinned-edit-btn {
        background: #18181b;
        border-color: rgba(255, 255, 255, 0.1);
        color: #a1a1aa;
      }
      body.dark .pinned-edit-btn:hover,
      [data-theme="dark"] .pinned-edit-btn:hover,
      .dark .pinned-edit-btn:hover {
        background: #27272a;
        border-color: rgba(255, 255, 255, 0.2);
        color: #f4f4f5;
      }
      body.dark .pinned-guideline-chip,
      [data-theme="dark"] .pinned-guideline-chip,
      .dark .pinned-guideline-chip {
        background: rgba(99, 102, 241, 0.15);
        border-color: rgba(99, 102, 241, 0.3);
        color: #818cf8;
      }
      body.dark .pinned-scope-chip,
      [data-theme="dark"] .pinned-scope-chip,
      .dark .pinned-scope-chip {
        background: #18181b;
        border-color: rgba(255, 255, 255, 0.1);
        color: #f4f4f5;
      }

      body.dark .workspace-popover,
      [data-theme="dark"] .workspace-popover,
      .dark .workspace-popover {
        background: #18181b;
        color: #f4f4f5;
        border-color: rgba(255, 255, 255, 0.12);
        box-shadow: 0 16px 40px rgba(0, 0, 0, 0.6), 0 2px 8px rgba(0, 0, 0, 0.3);
      }
      body.dark .ws-popover-path-bar,
      [data-theme="dark"] .ws-popover-path-bar,
      .dark .ws-popover-path-bar {
        background: rgba(255, 255, 255, 0.05);
        border-color: rgba(255, 255, 255, 0.08);
        color: #a1a1aa;
      }
      body.dark .ws-popover-item:hover,
      [data-theme="dark"] .ws-popover-item:hover,
      .dark .ws-popover-item:hover {
        background: rgba(255, 255, 255, 0.06);
      }
      body.dark .ws-form-input,
      [data-theme="dark"] .ws-form-input,
      .dark .ws-form-input {
        background: #27272a;
        border-color: rgba(255, 255, 255, 0.14);
        color: #f4f4f5;
      }
      body.dark .ws-remote-preview,
      [data-theme="dark"] .ws-remote-preview,
      .dark .ws-remote-preview {
        background: rgba(255, 255, 255, 0.04);
        border-color: rgba(255, 255, 255, 0.08);
      }

      body.dark .pinned-edit-btn.is-open,
      [data-theme="dark"] .pinned-edit-btn.is-open,
      .dark .pinned-edit-btn.is-open {
        background: #27272a;
        border-color: var(--accent, #6366f1);
        box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.25);
        color: #818cf8;
      }
      body.dark .pinned-popover,
      [data-theme="dark"] .pinned-popover,
      .dark .pinned-popover {
        background: #18181b;
        color: #f4f4f5;
        border-color: rgba(255, 255, 255, 0.12);
        box-shadow: 0 16px 40px rgba(0, 0, 0, 0.6), 0 2px 8px rgba(0, 0, 0, 0.3);
      }
      body.dark .pinned-popover-scope-row,
      [data-theme="dark"] .pinned-popover-scope-row,
      .dark .pinned-popover-scope-row {
        background: #202024;
        border-color: rgba(255, 255, 255, 0.08);
      }
      body.dark .pinned-popover-scope-row:hover,
      [data-theme="dark"] .pinned-popover-scope-row:hover,
      .dark .pinned-popover-scope-row:hover {
        border-color: rgba(99, 102, 241, 0.35);
        background: #27272a;
      }
      body.dark .pinned-popover-scope-row .pinned-scope-type,
      body.dark .pinned-popover-scope-row .pinned-scope-path,
      body.dark .pinned-popover-scope-row .pinned-scope-label,
      [data-theme="dark"] .pinned-popover-scope-row .pinned-scope-type,
      [data-theme="dark"] .pinned-popover-scope-row .pinned-scope-path,
      [data-theme="dark"] .pinned-popover-scope-row .pinned-scope-label,
      .dark .pinned-popover-scope-row .pinned-scope-type,
      .dark .pinned-popover-scope-row .pinned-scope-path,
      .dark .pinned-popover-scope-row .pinned-scope-label {
        background: #18181b;
        border-color: rgba(255, 255, 255, 0.12);
        color: #f4f4f5;
      }
      body.dark .pinned-popover-guideline-hint,
      [data-theme="dark"] .pinned-popover-guideline-hint,
      .dark .pinned-popover-guideline-hint {
        background: rgba(99, 102, 241, 0.12);
        border-color: rgba(99, 102, 241, 0.25);
        color: #a5b4fc;
      }
      body.dark .pinned-popover-footer,
      [data-theme="dark"] .pinned-popover-footer,
      .dark .pinned-popover-footer {
        background: #18181b;
        border-top-color: rgba(255, 255, 255, 0.08);
      }
      body.dark .pinned-guideline-input,
      [data-theme="dark"] .pinned-guideline-input,
      .dark .pinned-guideline-input {
        background: #27272a;
        border-color: rgba(255, 255, 255, 0.14);
        color: #f4f4f5;
      }
      body.dark .pinned-btn-secondary,
      [data-theme="dark"] .pinned-btn-secondary,
      .dark .pinned-btn-secondary {
        background: #27272a;
        border-color: rgba(255, 255, 255, 0.12);
        color: #f4f4f5;
      }
      body.dark .pinned-btn-secondary:hover,
      [data-theme="dark"] .pinned-btn-secondary:hover,
      .dark .pinned-btn-secondary:hover {
        background: #3f3f46;
        border-color: rgba(255, 255, 255, 0.2);
        color: #ffffff;
      }

      /* 基础按钮与通用 */
      .pinned-btn-secondary,
      .pinned-btn-primary {
        padding: 6px 14px;
        font-size: 13px;
        border-radius: 6px;
        cursor: pointer;
        border: 1px solid var(--border-light, rgba(0,0,0,0.14));
      }
      .pinned-btn-secondary {
        background: var(--bg-card, #fff);
        color: var(--text-primary, #222);
      }
      .pinned-btn-primary {
        background: var(--accent, #6366f1);
        color: #fff;
        border-color: transparent;
        font-weight: 500;
      }
      .pinned-btn-primary:disabled,
      .pinned-btn-secondary:disabled {
        opacity: 0.55;
        cursor: not-allowed;
      }

      /* 既有 Scope 编辑表单 */
      .pinned-section {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .pinned-section-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
      }
      .pinned-section-label {
        font-size: 13px;
        font-weight: 500;
      }
      .pinned-section-hint {
        font-size: 11.5px;
        font-weight: 400;
        color: var(--text-secondary, #666);
        margin-left: 6px;
      }
      .pinned-guideline-input {
        width: 100%;
        font-family: inherit;
        font-size: 13px;
        padding: 9px 11px;
        border-radius: 8px;
        border: 1px solid var(--border-light, rgba(0,0,0,0.14));
        background: var(--bg-card, #fff);
        color: inherit;
        resize: vertical;
        min-height: 80px;
      }
      .pinned-add-btn {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 4px 10px;
        font-size: 12px;
        border-radius: 6px;
        border: 1px solid var(--border-light, rgba(0,0,0,0.14));
        background: var(--bg-card, #fff);
        cursor: pointer;
        color: inherit;
      }
      .pinned-add-btn:hover {
        background: var(--bg-elevated, rgba(0,0,0,0.04));
      }
      .pinned-empty-hint {
        font-size: 12px;
        color: var(--text-secondary, #777);
        padding: 8px 0;
        font-style: italic;
      }
      .pinned-scope-table {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .pinned-scope-row {
        display: grid;
        grid-template-columns: 110px 1fr 130px 28px;
        gap: 6px;
        align-items: center;
      }
      .pinned-scope-type,
      .pinned-scope-path,
      .pinned-scope-label {
        font-family: inherit;
        font-size: 12.5px;
        padding: 6px 8px;
        border-radius: 6px;
        border: 1px solid var(--border-light, rgba(0,0,0,0.14));
        background: var(--bg-card, #fff);
        color: inherit;
        min-width: 0;
      }
      .pinned-scope-path {
        font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      }
      .pinned-icon-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 4px;
        border: none;
        background: transparent;
        border-radius: 6px;
        cursor: pointer;
        color: var(--text-secondary, #666);
      }
      .pinned-icon-btn:hover {
        background: var(--bg-elevated, rgba(0,0,0,0.06));
        color: var(--text-primary, #222);
      }
      .pinned-error {
        color: #ef4444;
        font-size: 12px;
        padding: 4px 0;
      }
    `}</style>
  )
}
