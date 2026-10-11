/* ============================================================================
   「辅助模型」区——目前只放"翻译模型"。

   设计动机：思考块「翻译」按钮当主对话用 reasoning model（Claude 4 / o-series /
   DeepSeek-R1）时容易把待译思考文本当指令理解 → 翻译跑题。把翻译指给一个
   独立的 fast non-reasoning model 能彻底规避；不指定时后端用
   `use_disable_thinking()` 关 reasoning 兜底。

   2026-05-27：从 ProviderSettings 底部迁出到「辅助功能」面板（原「语音输入」），
   跟语音转写并列，避免「模型服务」tab 越塞越长。组件不再依赖外部传入
   providers，自己 listProviders() + 监听 MODELS_CATALOG_CHANGED_EVENT 刷新。

   未来扩展：可在同一区段加「话题命名模型」「记忆压缩模型」等其他辅助任务
   model 槽位（对标 通用供应商规范 的 quickModel / translateModel 并列设计）。
   ============================================================================ */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Languages, Loader2 } from 'lucide-react'
import {
  getTranslateModel,
  listProviders,
  setTranslateModel,
  type ActiveModelView,
  type ProviderView,
} from '../../api/providers'
import { MODELS_CATALOG_CHANGED_EVENT } from '../../api/chat'

export default function AuxModelsFooter() {
  const [providers, setProviders] = useState<ProviderView[]>([])
  const [translateMv, setTranslateMv] = useState<ActiveModelView>({ provider_id: '', model: '' })
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  // 拉 providers 列表（用于下拉选项）。失败时 UI 仍可用，只是选项为空。
  const refreshProviders = useCallback(async () => {
    try {
      const resp = await listProviders()
      setProviders(resp.providers ?? [])
    } catch {
      /* 静默——后端未起 / 网络错时不阻塞辅助功能页其他配置 */
    }
  }, [])

  useEffect(() => {
    void refreshProviders()
    // ProviderSettings 改动后会 dispatch 此事件，监听以保持选项同步
    const onChanged = () => { void refreshProviders() }
    window.addEventListener(MODELS_CATALOG_CHANGED_EVENT, onChanged)
    return () => window.removeEventListener(MODELS_CATALOG_CHANGED_EVENT, onChanged)
  }, [refreshProviders])

  // 拉当前翻译模型配置（404/网络错按未配置处理；后端没接 endpoint 时也不让 UI 挂）
  useEffect(() => {
    let cancelled = false
    getTranslateModel()
      .then((mv) => { if (!cancelled) setTranslateMv(mv) })
      .catch(() => { /* 静默——未配置 / 端点未上线时按 "" 处理 */ })
    return () => { cancelled = true }
  }, [])

  // 平铺 provider × model 选项；只列 enabled provider 的模型，按 provider 分组
  // 用 "<provider_id>::<model_id>" 编码 select 的 value，避免重名 model 撞车。
  const enabledProviders = useMemo(
    () => providers.filter((p) => p.enabled !== false && p.models.length > 0),
    [providers],
  )

  const value = translateMv.provider_id && translateMv.model
    ? `${translateMv.provider_id}::${translateMv.model}`
    : ''

  const onChange = useCallback(async (raw: string) => {
    setErr(null)
    setSaving(true)
    try {
      if (!raw) {
        const mv = await setTranslateModel({ provider_id: '', model: '' })
        setTranslateMv(mv)
        return
      }
      const idx = raw.indexOf('::')
      if (idx < 0) return
      const provider_id = raw.slice(0, idx)
      const model = raw.slice(idx + 2)
      const mv = await setTranslateModel({ provider_id, model })
      setTranslateMv(mv)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }, [])

  return (
    <section className="settings-models-aux" aria-label="辅助模型">
      <div className="settings-models-aux__head">
        <Languages size={14} />
        <span>辅助模型</span>
      </div>
      <div className="settings-models-aux__row">
        <label htmlFor="lr-aux-translate">翻译模型</label>
        <select
          id="lr-aux-translate"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={saving}
        >
          <option value="">— 跟随当前会话 model（默认）—</option>
          {enabledProviders.map((p) => (
            <optgroup key={p.id} label={p.name || p.id}>
              {p.models.map((m) => (
                <option key={`${p.id}::${m.id}`} value={`${p.id}::${m.id}`}>
                  {m.name || m.id}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        {saving && <Loader2 size={14} className="spin" />}
      </div>
      <div className="settings-models-aux__hint">
        思考块「翻译」按钮使用此模型。<strong>建议挑一个非 reasoning 的 fast model</strong>
        （如 <code>gpt-4o-mini</code> / <code>qwen-turbo</code> / <code>deepseek-chat</code>）——
        主对话是 reasoning model（Claude 4 / o-series / DeepSeek-R1）时翻译用同一 model 容易跑题。
        留空则跟随当前会话 model，后端会自动关 reasoning 兜底。
      </div>
      {err && <div className="settings-models-aux__hint" style={{ color: 'var(--color-danger, #d33)' }}>{err}</div>}
    </section>
  )
}
