/**
 * 自定义请求头编辑弹窗。
 *
 * 对齐参考：
 *   - Cherry Studio `_research/cherry-studio-main/src/renderer/src/pages/settings/ProviderSettings/CustomHeaderPopup.tsx`
 *
 * 用途：聚合 router (OpenRouter / getrouter / "春风"/微步) 等会按 User-Agent /
 * HTTP-Referer / X-Title 鉴别合法客户端，缺则返 403 "Your request was blocked"。
 * 项目级默认头已在 backend/utils/llm_headers.py 注入（伪装成 Cherry-style UA），
 * 极少数特殊 router 需要用户在这里**手动覆盖**为它们要求的具体 headers。
 *
 * UI 形态：JSON 文本编辑（不做 key/value 行编辑）—— Cherry 同款，简单直接，
 * 用户能直接复制粘贴官方文档示例。
 */
import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { createPortal } from 'react-dom'
import { AlertCircle, Check, Loader2, RotateCcw, Trash2, X } from 'lucide-react'

import { getDefaultHeaders, updateProvider, type ProviderView } from '../../api/providers'

interface ExtraHeadersDialogProps {
  provider: ProviderView
  onClose: () => void
  onSaved: () => void
}

export default function ExtraHeadersDialog({
  provider, onClose, onSaved,
}: ExtraHeadersDialogProps) {
  // 两阶段初始化：
  // 1. 同步阶段先用 provider.extra_headers 塑 textarea，避免闪白
  // 2. 异步 fetch 项目默认 baseline（UA 是后端动态生成，前端无法还原）
  // 3. baseline 到手后，用 baseline + override 合并后的完整 headers 填 textarea，
  //    让用户看到「实际发出去的样子」，所见即所得
  const initialOverride = provider.extra_headers ?? {}

  const [baseline, setBaseline] = useState<Record<string, string> | null>(null)
  const [text, setText] = useState<string>(
    JSON.stringify(initialOverride, null, 2) === '{}'
      ? '{}'
      : JSON.stringify(initialOverride, null, 2),
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loadingBaseline, setLoadingBaseline] = useState(true)

  // 入口拉 baseline。一旦到手，把 textarea 刷成 baseline + override 的完整在出。
  // 只在 mount 时跑一次；provider 变了也不重拉（baseline 不随 provider 变）。
  useEffect(() => {
    let alive = true
    getDefaultHeaders(provider.transport)
      .then((res) => {
        if (!alive) return
        setBaseline(res.headers)
        // 把完整合并后的 headers 塑进 textarea 作为初始内容——用户一开就能看到东西可参考。
        // 仅在用户还没动过 textarea（initialOverride 是空）时覆盖，避免护他刚输入的内容。
        if (Object.keys(initialOverride).length === 0) {
          setText(JSON.stringify(res.headers, null, 2))
        }
      })
      .catch((e) => {
        console.warn('[ExtraHeadersDialog] 拉项目默认 headers 失败', e)
        // 失败不阻塞使用，baseline 保持 null，diff 逻辑倒退为‌原始全量保存
      })
      .finally(() => {
        if (alive) setLoadingBaseline(false)
      })
    return () => { alive = false }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 跨协议旧请求头检测（如 Anthropic 下却残留 codex 头）
  const hasStaleCrossProtocolHeaders = useMemo(() => {
    if (!provider.extra_headers || Object.keys(provider.extra_headers).length === 0) return false
    const keys = Object.keys(provider.extra_headers)
    const vals = Object.values(provider.extra_headers)
    const isCodex = keys.some(k => k.toLowerCase().includes('codex')) || vals.some(v => typeof v === 'string' && v.toLowerCase().includes('codex'))
    const isAnthropic = keys.some(k => k.toLowerCase().includes('anthropic')) || vals.some(v => typeof v === 'string' && v.toLowerCase().includes('claude'))
    if (provider.transport === 'anthropic_messages' && isCodex) return 'openai'
    if (provider.transport === 'openai_chat' && isAnthropic) return 'anthropic'
    return false
  }, [provider.extra_headers, provider.transport])

  // Esc 关闭，对齐 AddModelDialog 习惯
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // 实时校验 JSON：用户每改一次都解析一次，错误就显示提示但不阻塞编辑
  // （让用户在 textarea 里能看到自己半成品的内容）
  let parsed: Record<string, string> | null = null
  let parseError: string | null = null
  try {
    const trimmed = text.trim()
    if (trimmed === '') {
      parsed = {}
    } else {
      const obj = JSON.parse(trimmed)
      if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
        parseError = 'JSON 必须是对象（{...}），不能是数组/null/原值'
      } else {
        // 校验所有 value 都是 string —— HTTP header 不允许 number/bool
        const bad = Object.entries(obj).find(([, v]) => typeof v !== 'string')
        if (bad) {
          parseError = `字段 "${bad[0]}" 的值必须是字符串`
        } else {
          parsed = obj as Record<string, string>
        }
      }
    }
  } catch (e) {
    parseError = e instanceof Error ? e.message : 'JSON 解析失败'
  }

  const canSubmit = !saving && parseError === null

  // 保存前自动 diff：跟 baseline 完全一致的 key 不存 extra_headers（避免冗余）。
  // 这样用户“未修改全默认 headers” 与 “明确清空 override” 语义一致，所存仅为真差异。
  const diffFromBaseline = (full: Record<string, string>): Record<string, string> => {
    if (!baseline) return full
    const out: Record<string, string> = {}
    for (const [k, v] of Object.entries(full)) {
      if (baseline[k] !== v) out[k] = v  // 同 key 但 value 不同 → 存；new key 也存
    }
    return out
  }

  const onSubmit = async () => {
    if (!canSubmit || parsed === null) return
    setSaving(true)
    setError(null)
    try {
      const toSave = diffFromBaseline(parsed)
      await updateProvider(provider.id, { extra_headers: toSave })
      onSaved()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  // 重置为项目默认 baseline——让 textarea 恢复成 “项目原始 headers” 可边看边改。
  const resetToBaseline = () => {
    if (!baseline) return
    setText(JSON.stringify(baseline, null, 2))
  }

  // 清空 override——让 textarea 变 "{}"，provider 完全用项目默认。
  // 注意跟「重置」语义不同：重置是“顶 textarea 填入 baseline 为可见参考”，
  // 清空是“让保存后的实际 override 为空 dict”。这两个动作保存后效果一样（diff 都是 {})，
  // 但 textarea 显示不同；留两个是因为用户意图不同。
  const clearAll = () => {
    setText('{}')
  }

  // overlay click 关闭：复刻 AddModelDialog 的 mousedown/mouseup 配对模式
  // （避免拖动选中文字松手时误关闭）
  const downOnOverlayRef = useRef(false)
  const onOverlayMouseDown = (e: MouseEvent<HTMLDivElement>) => {
    downOnOverlayRef.current = e.target === e.currentTarget
  }
  const onOverlayMouseUp = (e: MouseEvent<HTMLDivElement>) => {
    if (downOnOverlayRef.current && e.target === e.currentTarget) onClose()
    downOnOverlayRef.current = false
  }

  // 「实际 override 项」计数：以 diff 后为准，避免 textarea 里连同 baseline 一起填了被误计。
  // baseline 未到手时退化为原始计数。
  const headerCount = useMemo(() => {
    if (!parsed) return 0
    return Object.keys(diffFromBaseline(parsed)).length
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [parsed, baseline])

  const node = (
    <div className="lr-mm-overlay" onMouseDown={onOverlayMouseDown} onMouseUp={onOverlayMouseUp}>
      <div className="lr-mm-panel" style={{ width: 'min(560px, 94vw)' }}>
        <header className="lr-mm-header">
          <div className="lr-mm-header__text">
            <div className="lr-mm-header__title">自定义请求头</div>
            <div className="lr-mm-header__sub">
              {provider.name} · {loadingBaseline ? '加载中...' : `${headerCount} 项自定义覆盖`}
            </div>
          </div>
          <button type="button" className="lr-mm-close" onClick={onClose} title="关闭 (Esc)">
            <X size={16} />
          </button>
        </header>

        <div className="lr-addform">
          <div className="lr-addform__field">
            <label className="lr-addform__label">Headers (JSON 对象)</label>
            {hasStaleCrossProtocolHeaders && (
              <div className="lr-addform__err" style={{ marginBottom: 8, background: 'var(--amber-subtle, #fff8c5)', borderColor: 'var(--amber-border, #d4a72c)', color: 'var(--amber-fg, #7d4e00)' }}>
                <AlertCircle size={12} />
                检测到当前包含 {hasStaleCrossProtocolHeaders === 'openai' ? 'OpenAI / Codex' : 'Anthropic'} 协议的历史请求头，可能会覆盖当前协议默认伪装。建议点击下方「重置为默认」一键恢复。
              </div>
            )}
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              spellCheck={false}
              autoFocus
              rows={10}
              style={{
                fontFamily: 'ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace',
                fontSize: '12px',
                lineHeight: 1.5,
                resize: 'vertical',
                width: '100%',
                padding: '8px 10px',
                borderRadius: 6,
                border: '1px solid var(--border-color, #d0d7de)',
                background: 'var(--input-bg, #fff)',
                color: 'var(--text-color, #24292f)',
              }}
              placeholder={'{\n  "Header-Name": "Header-Value"\n}'}
            />
            {parseError && (
              <div className="lr-addform__err">
                <AlertCircle size={11} /> {parseError}
              </div>
            )}
            <div className="lr-addform__hint">
              上方默认填入的是**项目现在发请求时实际带的 headers**。
              跟默认一致的项保存时会自动过滤，存入 provider 的只是你改动那几项，所以随便改。<br />
              不知道怎么填就别动，项目默认值已能应付绝大多数 router。
            </div>
            <div style={{ marginTop: 8, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <button
                type="button"
                className="lr-mm-btn ghost"
                onClick={resetToBaseline}
                disabled={saving || loadingBaseline || !baseline}
                style={{ fontSize: 11 }}
                title="重新填入项目默认值作为可编辑参考"
              >
                <RotateCcw size={11} /> 重置默认
              </button>
              <button
                type="button"
                className="lr-mm-btn ghost"
                onClick={clearAll}
                disabled={saving}
                style={{ fontSize: 11 }}
                title="清空 textarea；保存后 provider 完全使用项目默认头"
              >
                <Trash2 size={11} /> 清空
              </button>
            </div>
          </div>

          {error && (
            <div className="lr-addform__err lr-addform__err--block">
              <AlertCircle size={13} /> {error}
            </div>
          )}
        </div>

        <footer className="lr-mm-footer">
          <div className="lr-mm-footer__actions">
            <button type="button" className="lr-mm-btn ghost" onClick={onClose} disabled={saving}>取消</button>
            <button type="button" className="lr-mm-btn primary" onClick={onSubmit} disabled={!canSubmit}>
              {saving ? <Loader2 size={13} className="spin" /> : <Check size={13} />}
              保存
            </button>
          </div>
        </footer>
      </div>
    </div>
  )

  return createPortal(node, document.body)
}
