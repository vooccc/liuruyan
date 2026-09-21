/**
 * SaveAsTemplateDialog — 角色实例"另存为模板"弹窗
 *
 * 设计：
 * - 受 CharacterWorkshop 控制（open / agentId / 当前 data 用于预填默认 template_id）
 * - template_id 必须符合后端 _TEMPLATE_ID_RE：^[a-z][a-z0-9_]{1,31}$
 * - 默认两个选项 checkbox（包含记忆 / 包含 USER.md）均 false——和后端默认一致
 * - 后端返回 400 + TEMPLATE_VALIDATION_FAILED 时，把 errors 清单完整渲染出来，并给"去 Workshop 补全"提示
 * - 409 提示用户改名（不暴露 overwrite，避免用户误覆盖自己之前保存的模板；如需覆盖，二期再加）
 */
import { useEffect, useMemo, useState } from 'react'
import { X, Save, Loader2, AlertTriangle, Info } from 'lucide-react'
import { saveAgentAsTemplate, SaveAsTemplateError, Template } from '../../api/chat'

interface SaveAsTemplateDialogProps {
  open: boolean
  onClose: () => void
  /** 当前正在编辑的 agent id（必传） */
  agentId: string
  /** 用来预填默认 template_id 候选（拿 agent 名转 ascii） */
  agentName?: string
  /** 保存成功回调，参数是后端返回的新模板 meta */
  onSaved?: (tmpl: Template) => void
}

/** 拼一个合法 template_id 默认值（中文名转拼音不现实，给个时间戳形态） */
function defaultTemplateIdFrom(name?: string): string {
  // 只挑保留 ascii 字母数字；落空就用 my_role
  const ascii = (name || '').toLowerCase().replace(/[^a-z0-9_]/g, '')
  const base = ascii && /^[a-z]/.test(ascii) ? ascii.slice(0, 24) : 'my_role'
  // 加一个 4 位短时间戳后缀，降低重名概率
  const suffix = Math.floor(Date.now() / 1000).toString(36).slice(-4)
  return `${base}_${suffix}`
}

const TEMPLATE_ID_PATTERN = /^[a-z][a-z0-9_]{1,31}$/

export default function SaveAsTemplateDialog({
  open,
  onClose,
  agentId,
  agentName,
  onSaved,
}: SaveAsTemplateDialogProps) {
  const [templateId, setTemplateId] = useState('')
  const [includeMemory, setIncludeMemory] = useState(false)
  const [includeUser, setIncludeUser] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [validationErrors, setValidationErrors] = useState<string[] | null>(null)

  // 打开时重置表单，并塞一个建议的 template_id
  useEffect(() => {
    if (!open) return
    setTemplateId(defaultTemplateIdFrom(agentName))
    setIncludeMemory(false)
    setIncludeUser(false)
    setError(null)
    setValidationErrors(null)
  }, [open, agentName])

  const idLocalError = useMemo(() => {
    if (!templateId) return null
    if (!TEMPLATE_ID_PATTERN.test(templateId)) {
      return '必须以小写字母开头，仅含小写字母/数字/下划线，长度 2-32 字符'
    }
    return null
  }, [templateId])

  const canSubmit = !!templateId && !idLocalError && !saving

  async function handleSubmit() {
    if (!canSubmit) return
    setSaving(true)
    setError(null)
    setValidationErrors(null)
    try {
      const tmpl = await saveAgentAsTemplate(agentId, {
        template_id: templateId.trim(),
        include_memory: includeMemory,
        include_user: includeUser,
      })
      onSaved?.(tmpl)
      onClose()
    } catch (e) {
      const err = e as SaveAsTemplateError
      if (err.status === 400 && err.code === 'TEMPLATE_VALIDATION_FAILED' && err.errors?.length) {
        setValidationErrors(err.errors)
        setError(err.message || '模板校验未通过')
      } else if (err.status === 409) {
        setError(err.message || '该 template_id 已被使用，请换一个名字')
      } else {
        setError(err.message || '保存失败')
      }
    } finally {
      setSaving(false)
    }
  }

  if (!open) return null

  return (
    <div
      className="workshop-overlay save-as-template-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !saving) onClose()
      }}
    >
      <div className="workshop-panel save-as-template-panel">
        <header className="workshop-header">
          <div className="workshop-header-left">
            <h2 className="workshop-title">另存为角色模板</h2>
          </div>
          <div className="workshop-header-right">
            <button className="btn-close-workshop" onClick={onClose} disabled={saving} type="button">
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="save-as-template-body">
          <div className="save-as-template-intro">
            <Info size={16} className="save-as-template-intro__icon" />
            <span>
              将当前角色冻结为新模板（含 IDENTITY / SOUL / PERSONALITY / 头像），
              之后可以根据该模板创建新的角色实例。当前角色不受影响。
            </span>
          </div>

          {/* template_id 输入 */}
          <div className="form-group">
            <label className="form-label" htmlFor="save-as-template-id">
              模板ID <span className="form-label-hint">英文小写，作为模板在磁盘上的唯一目录名</span>
            </label>
            <input
              id="save-as-template-id"
              className={`form-input save-as-template-id-input${idLocalError ? ' has-error' : ''}`}
              type="text"
              value={templateId}
              onChange={(e) => setTemplateId(e.target.value.trim())}
              placeholder="例如：my_warrior_v2"
              disabled={saving}
              autoFocus
              spellCheck={false}
            />
            {idLocalError ? (
              <span className="save-as-template-hint save-as-template-hint--error">{idLocalError}</span>
            ) : (
              templateId && (
                <span className="save-as-template-hint">
                  保存目录：<code>agents/templates/{templateId}/</code>
                </span>
              )
            )}
          </div>

          {/* 选项 */}
          <div className="save-as-template-options">
            <label className="save-as-template-option">
              <input
                type="checkbox"
                checked={includeMemory}
                onChange={(e) => setIncludeMemory(e.target.checked)}
                disabled={saving}
              />
              <span>
                <strong>长期记忆（MEMORY.md）</strong>
                <span className="save-as-template-option__hint">
                  包含具体场景与用户的个人化内容，默认"带前情"的模板。
                </span>
              </span>
            </label>

            <label className="save-as-template-option">
              <input
                type="checkbox"
                checked={includeUser}
                onChange={(e) => setIncludeUser(e.target.checked)}
                disabled={saving}
              />
              <span>
                <strong>用户档案（USER.md）</strong>
                <span className="save-as-template-option__hint">
                  角色对用户的认知，含具体的称呼 / 偏好等信息。
                </span>
              </span>
            </label>
          </div>

          {/* 错误显示 */}
          {error && (
            <div className="save-as-template-error">
              <AlertTriangle size={16} className="save-as-template-error__icon" />
              <div className="save-as-template-error__body">
                <div className="save-as-template-error__title">{error}</div>
                {validationErrors && (
                  <>
                    <ul className="save-as-template-error__list">
                      {validationErrors.map((msg, i) => (
                        <li key={i}>{msg}</li>
                      ))}
                    </ul>
                    <div className="save-as-template-error__hint">
                      请关闭本弹窗，回到工坊 <strong>📋 基础</strong> / <strong>📊 属性</strong> Tab 补全这些字段后再保存。
                    </div>
                  </>
                )}
              </div>
            </div>
          )}
        </div>

        {/* 底部操作栏 */}
        <div className="save-as-template-footer">
          <button
            className="btn-cancel"
            onClick={onClose}
            disabled={saving}
            type="button"
          >
            取消
          </button>
          <button
            className={`btn-save${saving ? ' saving' : ''}`}
            onClick={handleSubmit}
            disabled={!canSubmit}
            type="button"
          >
            {saving ? (
              <>
                <Loader2 size={15} className="spinning" />
                保存中...
              </>
            ) : (
              <>
                <Save size={15} />
                保存为模板
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  )
}
