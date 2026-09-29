/**
 * ExportImportDialog — 数据导入/导出弹窗
 *
 * 三个 Tab：
 * - 导出角色：勾选 N 个 agent → 后端写到 ~/.liuruyan/exports/ → 自动打开该文件夹
 * - 导出记忆：一键打包全局 memory → 同上
 * - 导入：拖拽或选择 ZIP → 后端按 manifest.kind 自动合并 → 报告
 *
 * 设计动机（导出走"写盘+开文件夹"而不是浏览器下载）：
 * Tauri webview 默认拦截 blob URL 下载，<a download> click 不触发。
 * 改为后端直接落盘 + explorer 打开目录，dev 浏览器和 Tauri 都通。
 *
 * 同 ID agent 走"合并"语义（角色设定以本机为准、新 session/日记补入）。
 */

import { useEffect, useState } from 'react'
import {
  X, Download, Upload, Package, Database, FileArchive, Loader2,
  CheckSquare, Square, FolderOpen,
} from 'lucide-react'
import {
  listAgentsForExport,
  exportAgentsToDisk,
  exportMemoryToDisk,
  openExportsDir,
  importArchive,
  isTauri,
  saveAsWithTauri,
  type AgentSummary,
  type ExportResult,
  type ImportReport,
} from '../api/exportImport'
import { getRuntime, type RuntimeKind } from '../runtime'

type Tab = 'export-agents' | 'export-memory' | 'import'

interface Props {
  open: boolean
  onClose: () => void
}

export default function ExportImportDialog({ open, onClose }: Props) {
  const [tab, setTab] = useState<Tab>('export-agents')
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [errMsg, setErrMsg] = useState('')
  const [importReport, setImportReport] = useState<ImportReport | null>(null)
  const [exportSuccess, setExportSuccess] = useState<ExportResult | null>(null)

  // M8a：runtime 探测。web 模式（PWA / 手机浏览器远程访问）下：
  //   - 导出按钮禁用：手机用户没法接收电脑那端 explorer 弹出的文件夹
  //   - 导入仍允许：浏览器 file input + FormData 标准能力，手机能正常用
  // 详见 _docs/REMOTE_M8A.md §5 Step 6
  const [runtimeKind, setRuntimeKind] = useState<RuntimeKind | null>(null)
  useEffect(() => {
    let alive = true
    void getRuntime()
      .then((r) => { if (alive) setRuntimeKind(r.kind) })
      .catch(() => { if (alive) setRuntimeKind('web') })  // 探测失败保守按 web 处理
    return () => { alive = false }
  }, [])
  const isWebMode = runtimeKind === 'web'

  // 加载 agent 列表
  useEffect(() => {
    if (!open || tab !== 'export-agents') return
    let alive = true
    setErrMsg('')
    listAgentsForExport()
      .then((data) => {
        if (!alive) return
        setAgents(data)
        setSelected(new Set(data.map((a) => a.agent_id))) // 默认全选
      })
      .catch((e) => {
        if (alive) setErrMsg(`加载失败：${(e as Error).message}`)
      })
    return () => {
      alive = false
    }
  }, [open, tab])

  // 切 tab 时清掉成功 / 报告状态（避免视觉残留）
  useEffect(() => {
    setExportSuccess(null)
    setImportReport(null)
    setErrMsg('')
  }, [tab])

  // ESC 关闭
  useEffect(() => {
    if (!open) return
    const h = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onClose()
    }
    document.addEventListener('keydown', h)
    return () => document.removeEventListener('keydown', h)
  }, [open, busy, onClose])

  if (!open) return null

  const allSelected = agents.length > 0 && selected.size === agents.length
  const noneSelected = selected.size === 0

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleAll = () => {
    if (allSelected) setSelected(new Set())
    else setSelected(new Set(agents.map((a) => a.agent_id)))
  }

  const handleExportAgents = async () => {
    if (selected.size === 0) {
      setErrMsg('至少勾选一个角色')
      return
    }
    setBusy(true)
    setErrMsg('')
    setExportSuccess(null)
    try {
      // 1. 让后端先写到 ~/.liuruyan/exports/（拿到稳定源路径）
      const result = await exportAgentsToDisk(Array.from(selected))

      // 2. Tauri 环境弹 save dialog 让用户选目标位置
      if (isTauri()) {
        try {
          const target = await saveAsWithTauri(result.path, result.filename)
          if (target === null) {
            // 用户取消 → 提示文件还在 ~/.liuruyan/exports/，可以再开
            setExportSuccess({ ...result, _userCancelled: true } as ExportResult & { _userCancelled?: boolean })
          } else {
            setExportSuccess({ ...result, path: target })
          }
        } catch (e) {
          setErrMsg(`保存失败：${(e as Error).message}（文件仍保留在 ${result.path}）`)
          setExportSuccess(result)
        }
      } else {
        // 浏览器 dev 模式 fallback：打开 exports/ 目录
        setExportSuccess(result)
        openExportsDir().catch(() => {})
      }
    } catch (e) {
      setErrMsg((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const handleExportMemory = async () => {
    setBusy(true)
    setErrMsg('')
    setExportSuccess(null)
    try {
      const result = await exportMemoryToDisk()

      if (isTauri()) {
        try {
          const target = await saveAsWithTauri(result.path, result.filename)
          if (target === null) {
            setExportSuccess({ ...result, _userCancelled: true } as ExportResult & { _userCancelled?: boolean })
          } else {
            setExportSuccess({ ...result, path: target })
          }
        } catch (e) {
          setErrMsg(`保存失败：${(e as Error).message}（文件仍保留在 ${result.path}）`)
          setExportSuccess(result)
        }
      } else {
        setExportSuccess(result)
        openExportsDir().catch(() => {})
      }
    } catch (e) {
      setErrMsg((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const handleImport = async (file: File) => {
    setBusy(true)
    setErrMsg('')
    setImportReport(null)
    try {
      const report = await importArchive(file)
      setImportReport(report)
    } catch (e) {
      setErrMsg((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const onPickFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) handleImport(file)
    e.target.value = '' // 让同一个文件能再选一次触发 onChange
  }

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault()
    const file = e.dataTransfer.files?.[0]
    if (file) handleImport(file)
  }

  return (
    <div className="confirm-overlay" onClick={() => !busy && onClose()}>
      <div className="confirm-dialog export-import-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="confirm-header" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <FileArchive size={20} className="confirm-icon info" />
            <span className="confirm-title">数据导入 / 导出</span>
          </div>
          <button className="eid-close-btn" onClick={onClose} disabled={busy} aria-label="关闭">
            <X size={18} />
          </button>
        </div>

        {/* Tabs */}
        <div className="eid-tabs">
          <TabBtn active={tab === 'export-agents'} onClick={() => setTab('export-agents')}>
            <Package size={14} /> 导出角色
          </TabBtn>
          <TabBtn active={tab === 'export-memory'} onClick={() => setTab('export-memory')}>
            <Database size={14} /> 导出记忆
          </TabBtn>
          <TabBtn active={tab === 'import'} onClick={() => setTab('import')}>
            <Upload size={14} /> 导入
          </TabBtn>
        </div>

        {/* Body */}
        <div className="eid-body">
          {(tab === 'export-agents' || tab === 'export-memory') && isWebMode && (
            <div
              className="eid-web-notice"
              style={{
                marginBottom: 12,
                padding: '10px 14px',
                background: 'rgba(255,200,80,0.1)',
                border: '1px solid rgba(255,200,80,0.4)',
                borderRadius: 6,
                fontSize: 13,
                lineHeight: 1.5,
              }}
            >
              <strong>导出功能仅桌面版可用</strong>
              <div style={{ marginTop: 4, color: 'var(--text-muted, #8b949e)' }}>
                你正通过浏览器 / PWA 访问柳如烟（远程模式）。请回到电脑上的柳如烟桌面版执行导出，或先在桌面版导出后通过云盘 / IM 发到手机。
              </div>
            </div>
          )}
          {tab === 'export-agents' && (
            <ExportAgentsPane
              agents={agents}
              selected={selected}
              onToggle={toggle}
              onToggleAll={toggleAll}
              allSelected={allSelected}
              noneSelected={noneSelected}
            />
          )}
          {tab === 'export-memory' && <ExportMemoryPane />}
          {tab === 'import' && (
            <ImportPane onPickFile={onPickFile} onDrop={onDrop} report={importReport} busy={busy} />
          )}

          {exportSuccess && (() => {
            const cancelled = (exportSuccess as ExportResult & { _userCancelled?: boolean })._userCancelled
            return (
              <div className="eid-success-banner" style={{ marginTop: 14 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <FolderOpen size={16} />
                  <strong>{cancelled ? '已生成（未保存到指定位置）' : '已导出到本地'}</strong>
                </div>
                <div style={{ marginTop: 6 }}>
                  文件：<code>{exportSuccess.filename}</code>（{formatBytes(exportSuccess.size_bytes)}）
                </div>
                <div style={{ marginTop: 4 }}>
                  位置：<code>{exportSuccess.path}</code>
                </div>
                <button
                  className="eid-mini-btn"
                  style={{ marginTop: 8 }}
                  onClick={() => openExportsDir().catch(() => {})}
                >
                  <FolderOpen size={12} style={{ marginRight: 4, verticalAlign: 'middle' }} />
                  打开默认导出文件夹
                </button>
              </div>
            )
          })()}
        </div>

        {errMsg && <p className="eid-error-text">{errMsg}</p>}

        {/* Action button per tab */}
        <div className="confirm-actions" style={{ marginTop: 14 }}>
          {tab === 'export-agents' && (
            <button
              className="confirm-btn info"
              onClick={handleExportAgents}
              disabled={busy || selected.size === 0 || isWebMode}
              title={isWebMode ? '请在电脑桌面版柳如烟中使用导出' : undefined}
            >
              {busy ? <Loader2 size={14} className="eid-spin" /> : <Download size={14} />}
              <span style={{ marginLeft: 6 }}>导出 {selected.size} 个角色</span>
            </button>
          )}
          {tab === 'export-memory' && (
            <button
              className="confirm-btn info"
              onClick={handleExportMemory}
              disabled={busy || isWebMode}
              title={isWebMode ? '请在电脑桌面版柳如烟中使用导出' : undefined}
            >
              {busy ? <Loader2 size={14} className="eid-spin" /> : <Download size={14} />}
              <span style={{ marginLeft: 6 }}>导出全局记忆</span>
            </button>
          )}
          <button className="confirm-btn cancel" onClick={onClose} disabled={busy}>
            关闭
          </button>
        </div>
      </div>
    </div>
  )
}

// ─────────── 子组件 ───────────

function TabBtn({
  active, onClick, children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button onClick={onClick} className={`eid-tab ${active ? 'active' : ''}`}>
      {children}
    </button>
  )
}

function ExportAgentsPane({
  agents, selected, onToggle, onToggleAll, allSelected, noneSelected,
}: {
  agents: AgentSummary[]
  selected: Set<string>
  onToggle: (id: string) => void
  onToggleAll: () => void
  allSelected: boolean
  noneSelected: boolean
}) {
  if (agents.length === 0) {
    return <p className="eid-empty">暂无角色。</p>
  }
  return (
    <div>
      <div className="eid-list-toolbar">
        <span>共 {agents.length} 个角色，已选 {selected.size}</span>
        <div className="eid-toolbar-actions">
          <button className="eid-mini-btn" onClick={onToggleAll} title={allSelected ? '取消全选' : '全选'}>
            {allSelected ? <CheckSquare size={12} /> : <Square size={12} />}
            <span style={{ marginLeft: 4 }}>{allSelected ? '取消全选' : noneSelected ? '全选' : '全选'}</span>
          </button>
        </div>
      </div>
      <ul className="eid-agent-list">
        {agents.map((a) => {
          const isSel = selected.has(a.agent_id)
          return (
            <li
              key={a.agent_id}
              onClick={() => onToggle(a.agent_id)}
              className={`eid-agent-row ${isSel ? 'selected' : ''}`}
            >
              <input type="checkbox" checked={isSel} readOnly />
              <div className="eid-agent-info">
                <span className="eid-agent-name">{a.name}</span>
                <span className="eid-agent-meta">
                  {a.session_count} 个会话{a.last_active ? ` · ${formatDate(a.last_active)}` : ''}
                </span>
              </div>
              <code className="eid-agent-id">{a.agent_id}</code>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function ExportMemoryPane() {
  return (
    <div className="eid-info-pane">
      <p>把全局记忆数据打包成 ZIP，包括：</p>
      <ul>
        <li>社交关系图（角色间情感、互动历史）</li>
        <li>游戏化机制（成就、嫉妒、注意力等）</li>
        <li>跨角色全局日记</li>
      </ul>
      <p className="eid-hint">提示：单个角色的对话记录请从「导出角色」拿。</p>
    </div>
  )
}

function ImportPane({
  onPickFile, onDrop, report, busy,
}: {
  onPickFile: (e: React.ChangeEvent<HTMLInputElement>) => void
  onDrop: (e: React.DragEvent) => void
  report: ImportReport | null
  busy: boolean
}) {
  return (
    <div>
      {!report && (
        <label
          onDragOver={(e) => e.preventDefault()}
          onDrop={onDrop}
          className={`eid-dropzone ${busy ? 'busy' : ''}`}
        >
          {busy ? (
            <Loader2 size={32} className="eid-spin eid-dropzone-icon" />
          ) : (
            <Upload size={32} className="eid-dropzone-icon" />
          )}
          <span className="eid-dropzone-title">{busy ? '导入中…' : '点击选择 或 拖入 ZIP 文件'}</span>
          <span className="eid-dropzone-sub">
            同 ID 角色合并：设定以本机为准，新 session / 日记补入
          </span>
          <input type="file" accept=".zip" onChange={onPickFile} style={{ display: 'none' }} disabled={busy} />
        </label>
      )}
      {report && <ImportReportView report={report} />}
    </div>
  )
}

function ImportReportView({ report }: { report: ImportReport }) {
  const ok = report.errors.length === 0
  return (
    <div>
      <div className={`eid-report-banner ${ok ? 'ok' : 'err'}`}>
        <strong>{ok ? '✓ 导入完成' : '⚠ 导入完成，但有错误'}</strong>
        <span style={{ marginLeft: 8, opacity: 0.8 }}>类型：{report.kind}</span>
      </div>

      {report.kind === 'agents' && <Stat label="新拷入角色" items={report.new_agents} />}
      {report.kind === 'agents' && <Stat label="同 ID 合并的角色" items={report.merged_agents} />}
      {report.added_sessions.length > 0 && (
        <Stat label="补入的新会话" items={report.added_sessions} />
      )}
      {report.skipped_sessions.length > 0 && (
        <Stat label="跳过的已存在会话" items={report.skipped_sessions} dim />
      )}
      <p className="eid-report-stat dim" style={{ marginTop: 10 }}>
        新文件：{report.added_files_count} · 跳过：{report.skipped_files_count}
      </p>
      {report.errors.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <strong style={{ color: 'var(--accent-red)' }}>错误（{report.errors.length}）</strong>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {report.errors.map((e, i) => (
              <li key={i} style={{ color: 'var(--accent-red)', fontSize: 12 }}>
                {e}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function Stat({ label, items, dim }: { label: string; items: string[]; dim?: boolean }) {
  if (items.length === 0) return null
  return (
    <p className={`eid-report-stat ${dim ? 'dim' : ''}`}>
      <strong>{label}（{items.length}）：</strong>
      <span style={{ marginLeft: 4 }}>{items.join(', ')}</span>
    </p>
  )
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString()
  } catch {
    return iso
  }
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}
