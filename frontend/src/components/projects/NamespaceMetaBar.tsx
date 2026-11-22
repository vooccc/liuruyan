import { useEffect, useState } from 'react'
import { Archive, ArchiveRestore, FolderOpen, Pencil, Plus, Save, Tag, X } from 'lucide-react'
import { ProjectNamespace, updateNamespace } from '../../api/chat'

interface NamespaceMetaBarProps {
  project: ProjectNamespace
  agentId: string
  onUpdated: (updated: ProjectNamespace) => void
  onError: (msg: string) => void
}

/**
 * 项目元信息栏：展示与内联编辑名称、口语别名、工作区锚定路径、摘要和归档状态。
 * 别名与工作区直接决定对话无感自动识别的准确率。
 */
export default function NamespaceMetaBar({
  project,
  agentId,
  onUpdated,
  onError,
}: NamespaceMetaBarProps) {
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [name, setName] = useState(project.name)
  const [aliases, setAliases] = useState<string[]>(project.aliases || [])
  const [aliasInput, setAliasInput] = useState('')
  const [workspacePath, setWorkspacePath] = useState(project.workspace_path || '')
  const [summary, setSummary] = useState(project.summary || '')

  useEffect(() => {
    setEditing(false)
    setName(project.name)
    setAliases(project.aliases || [])
    setAliasInput('')
    setWorkspacePath(project.workspace_path || '')
    setSummary(project.summary || '')
  }, [project])

  function addAlias() {
    const val = aliasInput.trim()
    if (!val) return
    if (!aliases.includes(val)) {
      setAliases(prev => [...prev, val])
    }
    setAliasInput('')
  }

  function removeAlias(target: string) {
    setAliases(prev => prev.filter(a => a !== target))
  }

  async function handleSave() {
    const trimmedName = name.trim()
    if (!trimmedName) return
    setSaving(true)
    try {
      const pendingAlias = aliasInput.trim()
      const finalAliases = pendingAlias && !aliases.includes(pendingAlias)
        ? [...aliases, pendingAlias]
        : aliases
      const res = await updateNamespace(project.id, {
        name: trimmedName,
        aliases: finalAliases,
        workspace_path: workspacePath.trim(),
        summary: summary.trim(),
        agentId,
      })
      setAliases(res.namespace.aliases || [])
      setAliasInput('')
      setEditing(false)
      onUpdated(res.namespace)
    } catch (e) {
      onError(`更新项目元信息失败: ${(e as Error).message || e}`)
    }
    setSaving(false)
  }

  async function handleToggleArchive() {
    const nextStatus = project.status === 'archived' ? 'active' : 'archived'
    setSaving(true)
    try {
      const res = await updateNamespace(project.id, {
        status: nextStatus,
        agentId,
      })
      onUpdated(res.namespace)
    } catch (e) {
      onError(`切换归档状态失败: ${(e as Error).message || e}`)
    }
    setSaving(false)
  }

  const isArchived = project.status === 'archived'

  return (
    <div className="ns-meta-bar">
      {!editing ? (
        <div className="ns-meta-readonly">
          <div className="ns-meta-head">
            <div className="ns-meta-title-group">
              <h3 className="ns-meta-title">{project.name}</h3>
              {isArchived && <span className="ns-status-badge">已归档</span>}
            </div>
            <div className="ns-meta-actions">
              <button
                className="text-btn ns-btn-xs"
                onClick={() => setEditing(true)}
                title="编辑项目名称、别名、工作区与摘要"
              >
                <Pencil size={11} /> 设置
              </button>
              <button
                className="text-btn ns-btn-xs"
                onClick={() => void handleToggleArchive()}
                disabled={saving}
                title={isArchived ? '恢复为活跃项目（参与自动识别与索引）' : '软归档（暂停自动识别与索引，保留全部记录）'}
              >
                {isArchived ? <><ArchiveRestore size={11} /> 恢复</> : <><Archive size={11} /> 归档</>}
              </button>
            </div>
          </div>

          <div className="ns-meta-row">
            <span className="ns-meta-item" title="口语别名（对话提及任一别名即可无感识别）">
              <Tag size={12} />
              {project.aliases && project.aliases.length > 0 ? (
                <span className="ns-alias-list">
                  {project.aliases.map(a => (
                    <span key={a} className="ns-alias-chip">{a}</span>
                  ))}
                </span>
              ) : (
                <span className="ns-meta-empty">未设别名</span>
              )}
            </span>

            {project.workspace_path && (
              <span className="ns-meta-item ns-meta-path" title={`工作区锚定：${project.workspace_path}`}>
                <FolderOpen size={12} />
                <span>{project.workspace_path}</span>
              </span>
            )}
          </div>

          {project.summary && (
            <div className="ns-meta-summary" title={project.summary}>
              {project.summary.replace(/\*\*/g, '').replace(/`/g, '')}
            </div>
          )}
        </div>
      ) : (
        <div className="ns-meta-editor">
          <div className="ns-meta-grid">
            <label className="ns-field">
              <span>项目名称</span>
              <input
                className="namespace-new-input"
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="规范项目名"
              />
            </label>
            <label className="ns-field">
              <span>工作区锚定目录（可选）</span>
              <input
                className="namespace-new-input"
                value={workspacePath}
                onChange={e => setWorkspacePath(e.target.value)}
                placeholder="如：D:\workspace\柳如烟"
              />
            </label>
          </div>

          <label className="ns-field">
            <span>口语别名（回车添加，用于无感识别）</span>
            <div className="ns-alias-editor">
              {aliases.map(a => (
                <span key={a} className="ns-alias-chip editable">
                  {a}
                  <button type="button" onClick={() => removeAlias(a)} title="移除别名">
                    <X size={10} />
                  </button>
                </span>
              ))}
              <div className="ns-alias-add">
                <input
                  className="namespace-new-input"
                  value={aliasInput}
                  onChange={e => setAliasInput(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      addAlias()
                    }
                  }}
                  placeholder="输入别名后回车"
                />
                <button type="button" className="icon-btn-sm" onClick={addAlias} title="添加别名">
                  <Plus size={12} />
                </button>
              </div>
            </div>
          </label>

          <label className="ns-field">
            <span>一句话摘要（其他项目索引展示用）</span>
            <input
              className="namespace-new-input"
              value={summary}
              onChange={e => setSummary(e.target.value)}
              placeholder="自动随状态卡同步，也可手动精简"
            />
          </label>

          <div className="ns-meta-editor-footer">
            <button className="icon-btn-sm success" onClick={() => void handleSave()} disabled={saving || !name.trim()}>
              <Save size={12} /> {saving ? '保存中...' : '保存设置'}
            </button>
            <button className="icon-btn-sm" onClick={() => setEditing(false)} disabled={saving}>
              取消
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
