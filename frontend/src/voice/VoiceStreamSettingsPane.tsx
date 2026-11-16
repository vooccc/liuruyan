/**
 * VoiceStreamSettingsPane：流式语音输入设置（设置 → 辅助功能 → 「流式语音」子标签）。
 *
 * 沿用 VoiceTranscriptionPane 的交互模式：每次修改立即 PUT（无 dirty/save 双态），
 * 保存后 dispatch 'voice-config-updated' 让输入框按钮即时切换流式/旧版。
 * UI 复用 voice-settings-* 全套样式类（hero / section / radio-card），零新增 CSS。
 */
import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, AudioWaveform, CheckCircle2, Loader2 } from 'lucide-react'

import { listProviders, type ProviderView } from '../api/providers'
import {
  fetchVoiceStreamConfig,
  updateVoiceStreamConfig,
  type VoiceStreamConfigResponse,
} from './types'

/** 与 VoiceSettings.tsx 里 RadioCard 同款（那边未导出，这里是独立副本，删模块无残留）。 */
function RadioCard({
  checked,
  onChange,
  title,
  desc,
}: {
  checked: boolean
  onChange: () => void
  title: string
  desc: string
}) {
  return (
    <button type="button" className={`voice-radio-card ${checked ? 'checked' : ''}`} onClick={onChange}>
      <span className="voice-radio-dot" aria-hidden>
        {checked ? <span className="voice-radio-dot-inner" /> : null}
      </span>
      <span className="voice-radio-text">
        <span className="voice-radio-title">{title}</span>
        <span className="voice-radio-desc">{desc}</span>
      </span>
    </button>
  )
}

const MODEL_OPTIONS: Array<{ value: string; label: string; note: string }> = [
  {
    value: 'paraformer-realtime-v2',
    label: 'Paraformer Realtime v2（推荐）',
    note: '阿里云百炼实时识别，中文准、延迟低、成熟稳定',
  },
  {
    value: 'fun-asr-realtime',
    label: 'Fun-ASR Realtime',
    note: '支持对话上下文热词（未来接记忆系统做专有名词增强）',
  },
  {
    value: 'qwen-audio-3.1-asr-flash-streaming',
    label: 'Qwen-Audio ASR Flash Streaming',
    note: '千问实时识别，多语种自动检测',
  },
]

/** 引擎卡片：provider_id 层面的选择（云端协议 vs 本地模型）。 */
const ENGINE_OPTIONS: Array<{ value: string; label: string; desc: string }> = [
  {
    value: 'local',
    label: '本地 · SenseVoice（默认 · 免 key）',
    desc: '模型跑在本机 CPU，全离线、零成本、隐私不出机；首次需下载约 230MB 模型',
  },
  {
    value: 'dashscope',
    label: '云端 · DashScope 实时',
    desc: '阿里云百炼实时识别，延迟最低、最准；复用「模型服务」里的 DashScope key',
  },
]

const LANGUAGE_OPTIONS: Array<{ label: string; hints: string[]; desc: string }> = [
  { label: '自动检测', hints: [], desc: '中英混说 / 多语言场景推荐' },
  { label: '中文优先', hints: ['zh'], desc: '固定提示 zh，中文准确率更高' },
  { label: '英文优先', hints: ['en'], desc: '固定提示 en，英文准确率更高' },
]

export default function VoiceStreamSettingsPane() {
  const [status, setStatus] = useState<VoiceStreamConfigResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)
  const [saveOk, setSaveOk] = useState(false)
  // 纠错 provider 列表（模型服务里配的全部 enabled provider）
  const [providers, setProviders] = useState<ProviderView[]>([])
  // 纠错模型名草稿：输入不立即 PUT，blur / Enter 提交（避免每敲一个字打一次接口）
  const [modelDraft, setModelDraft] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listProviders()
      .then((res) => {
        if (!cancelled) setProviders(res.providers.filter((p) => p.enabled))
      })
      .catch(() => {
        /* 后端未起：纠错选择器退化为只有当前 provider 可见，不阻断主面板 */
      })
    return () => {
      cancelled = true
    }
  }, [])

  const refresh = useCallback(async () => {
    setLoading(true)
    setLoadErr(null)
    const data = await fetchVoiceStreamConfig()
    if (data) {
      setStatus(data)
    } else {
      setLoadErr('后端未响应（功能未挂载或服务未启动）')
    }
    setLoading(false)
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const apply = useCallback(
    async (patch: Parameters<typeof updateVoiceStreamConfig>[0]) => {
      setSaving(true)
      setSaveErr(null)
      setSaveOk(false)
      try {
        const result = await updateVoiceStreamConfig(patch)
        setStatus(result.stream_status)
        setSaveOk(true)
        // 通知输入框按钮即时切换（流式 ↔ 旧批量转写）
        window.dispatchEvent(new CustomEvent('voice-config-updated'))
        setTimeout(() => setSaveOk(false), 2000)
      } catch (e) {
        setSaveErr(e instanceof Error ? e.message : String(e))
      } finally {
        setSaving(false)
      }
    },
    [],
  )

  if (loading) {
    return (
      <div style={{ padding: 24 }}>
        <Loader2 size={16} className="spin" /> 加载流式语音配置…
      </div>
    )
  }

  if (loadErr || !status) {
    return (
      <div style={{ padding: 24, color: 'var(--accent-red)' }}>
        <AlertCircle size={16} /> {loadErr ?? '未知错误'}
        <button type="button" className="toolbar-action-btn" onClick={refresh} style={{ marginLeft: 12 }}>
          重试
        </button>
      </div>
    )
  }

  const enabled = status.enabled

  return (
    <div className="voice-settings">
      <header className="voice-settings-hero">
        <div className="voice-settings-hero-icon">
          <AudioWaveform size={20} />
        </div>
        <div className="voice-settings-hero-text">
          <h2>流式语音输入</h2>
          <p>
            边说边出字：麦克风音频实时送识别引擎，说完话已经打在输入框里。
            云端（DashScope）或本地（SenseVoice，免 key）二选一；
            开启后自动替换「语音转写」的录完再转写模式，不可用时自动回退。
          </p>
        </div>
      </header>

      <section className="voice-settings-section">
        <h3>开关</h3>
        <div className="voice-radio-group">
          <RadioCard
            checked={enabled}
            onChange={() => void apply({ enabled: true })}
            title="开启流式"
            desc="边说边出字（引擎见下方选择；云端复用模型服务里的 key）"
          />
          <RadioCard
            checked={!enabled}
            onChange={() => void apply({ enabled: false })}
            title="关闭"
            desc="回退旧的「录完再转写」模式，本模块不出现在界面上"
          />
        </div>
      </section>

      {enabled ? (
        <>
          <section className="voice-settings-section">
            <h3>识别引擎</h3>
            <div className="voice-radio-group">
              {ENGINE_OPTIONS.map((e) => (
                <RadioCard
                  key={e.value}
                  checked={status.provider === e.value}
                  onChange={() => void apply({ provider_id: e.value })}
                  title={e.label}
                  desc={e.desc}
                />
              ))}
            </div>
            {status.provider === 'local' ? (
              <p style={{ marginTop: 8, fontSize: 13, color: 'var(--text-secondary, #666)' }}>
                {status.local.ready ? (
                  <span style={{ color: 'var(--accent-green, #22c55e)', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                    <CheckCircle2 size={14} /> 本地模型已就绪：{status.local.model}
                  </span>
                ) : (
                  <span style={{ color: '#f59e0b', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                    <AlertCircle size={14} /> 模型未就绪：需要 model.int8.onnx / tokens.txt /
                    silero_vad.onnx 放在 {status.local.dir} 下（约 230MB，下载一次永久离线用）
                  </span>
                )}
              </p>
            ) : null}
          </section>

          {status.provider === 'dashscope' ? (
            <section className="voice-settings-section">
              <h3>识别模型</h3>
              <div className="voice-radio-group">
                {MODEL_OPTIONS.map((m) => (
                  <RadioCard
                    key={m.value}
                    checked={status.model === m.value}
                    onChange={() => void apply({ model: m.value })}
                    title={m.label}
                    desc={m.note}
                  />
                ))}
              </div>
            </section>
          ) : null}

          <section className="voice-settings-section">
            <h3>LLM 纠错润色</h3>
            <div className="voice-radio-group">
              <RadioCard
                checked={status.polish.enabled}
                onChange={() => void apply({ polish_enabled: true })}
                title="开启纠错"
                desc={`每句识别结果先过一遍 LLM 修错别字/标点再上屏（用 ${status.polish.provider_id} 的 ${status.polish.model}，失败自动回退原文）`}
              />
              <RadioCard
                checked={!status.polish.enabled}
                onChange={() => void apply({ polish_enabled: false })}
                title="关闭"
                desc="识别结果直接上屏，零额外延迟"
              />
            </div>
            {status.polish.enabled ? (
              <>
                <div
                  style={{ display: 'flex', gap: 10, marginTop: 12, alignItems: 'center', flexWrap: 'wrap' }}
                >
                  {(() => {
                    // 单下拉模式（同翻译模型）：optgroup 按 provider 分组，value 编码 provider::model
                    const curProvider = providers.find((p) => p.id === status.polish.provider_id)
                    const curHasModels = (curProvider?.models?.length ?? 0) > 0
                    const anyModels = providers.some((p) => (p.models?.length ?? 0) > 0)
                    const value = `${status.polish.provider_id}::${status.polish.model}`
                    const onPolishChange = (raw: string) => {
                      const idx = raw.indexOf('::')
                      if (idx < 0) return
                      const pid = raw.slice(0, idx)
                      const model = raw.slice(idx + 2)
                      if (model) {
                        void apply({ polish_provider_id: pid, polish_model: model })
                      } else {
                        // 自定义 provider 无模型列表：只切 provider，模型名走下方输入框
                        void apply({ polish_provider_id: pid })
                      }
                    }
                    if (!anyModels && !curHasModels) {
                      // 极少见：没有任何带模型列表的 provider → 退化为 provider 下拉 + 手填模型
                      return (
                        <>
                          <select
                            className="voice-select"
                            value={status.polish.provider_id}
                            onChange={(e) => void apply({ polish_provider_id: e.target.value })}
                            style={{ maxWidth: 260 }}
                          >
                            {providers.some((p) => p.id === status.polish.provider_id) ? null : (
                              <option value={status.polish.provider_id}>
                                {status.polish.provider_id}（当前）
                              </option>
                            )}
                            {providers.map((p) => (
                              <option key={p.id} value={p.id}>
                                {p.name}
                                {p.has_api_key ? '' : ' · 未配 Key'}
                              </option>
                            ))}
                          </select>
                          <input
                            type="text"
                            className="voice-select"
                            value={modelDraft ?? status.polish.model}
                            placeholder="纠错模型名"
                            onChange={(e) => setModelDraft(e.target.value)}
                            onBlur={() => {
                              const v = (modelDraft ?? '').trim()
                              if (v && v !== status.polish.model) void apply({ polish_model: v })
                              else setModelDraft(null)
                            }}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                            }}
                            style={{ maxWidth: 260 }}
                          />
                        </>
                      )
                    }
                    return (
                      <>
                        <select
                          className="voice-select"
                          value={value}
                          onChange={(e) => onPolishChange(e.target.value)}
                          style={{ maxWidth: 360 }}
                        >
                          {/* 当前配置不在任何分组里（provider 被禁用/删除，或模型手填）→ 兜底项 */}
                          {providers.some(
                            (p) =>
                              p.id === status.polish.provider_id &&
                              (p.models ?? []).some((m) => m.id === status.polish.model),
                          ) ? null : (
                            <option value={value}>
                              {status.polish.provider_id} · {status.polish.model}（当前）
                            </option>
                          )}
                          {providers.map((p) =>
                            (p.models?.length ?? 0) > 0 ? (
                              <optgroup
                                key={p.id}
                                label={`${p.name}${p.has_api_key ? '' : ' · 未配 Key'}`}
                              >
                                {p.models!.map((m) => (
                                  <option key={`${p.id}::${m.id}`} value={`${p.id}::${m.id}`}>
                                    {m.name || m.id}
                                  </option>
                                ))}
                              </optgroup>
                            ) : (
                              <option key={p.id} value={`${p.id}::`}>
                                {p.name}（无模型列表 · 手动输入）
                              </option>
                            ),
                          )}
                        </select>
                        {/* 当前 provider 无模型列表（自定义中转）→ 模型名手动输入 */}
                        {curProvider && !curHasModels ? (
                          <input
                            type="text"
                            className="voice-select"
                            value={modelDraft ?? status.polish.model}
                            placeholder="纠错模型名"
                            onChange={(e) => setModelDraft(e.target.value)}
                            onBlur={() => {
                              const v = (modelDraft ?? '').trim()
                              if (v && v !== status.polish.model) void apply({ polish_model: v })
                              else setModelDraft(null)
                            }}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                            }}
                            style={{ maxWidth: 260 }}
                          />
                        ) : null}
                      </>
                    )
                  })()}
                </div>
                {(() => {
                  const sel = providers.find((p) => p.id === status.polish.provider_id)
                  if (sel && !sel.has_api_key) {
                    return (
                      <p
                        style={{
                          marginTop: 8,
                          fontSize: 13,
                          color: '#f59e0b',
                          display: 'flex',
                          alignItems: 'center',
                          gap: 6,
                        }}
                      >
                        <AlertCircle size={14} /> Provider「{sel.name}」还没配 API Key——去「模型服务」填好，
                        否则纠错会一直回退原文。
                      </p>
                    )
                  }
                  if (providers.length && !sel) {
                    return (
                      <p
                        style={{
                          marginTop: 8,
                          fontSize: 13,
                          color: '#f59e0b',
                          display: 'flex',
                          alignItems: 'center',
                          gap: 6,
                        }}
                      >
                        <AlertCircle size={14} /> 当前纠错 provider「{status.polish.provider_id}」不在
                        已启用列表里（可能被禁用或删除）——请在上方重新选择。
                      </p>
                    )
                  }
                  return null
                })()}
              </>
            ) : null}
            {status.polish.enabled && !status.polish.has_api_key && !providers.length ? (
              <p style={{ marginTop: 8, fontSize: 13, color: '#f59e0b', display: 'flex', alignItems: 'center', gap: 6 }}>
                <AlertCircle size={14} /> 纠错 provider「{status.polish.provider_id}」没配 key，
                目前会直接回退原文——去「模型服务」配置后自动生效。
              </p>
            ) : null}
          </section>

          <section className="voice-settings-section">
            <h3>语种</h3>
            <div className="voice-radio-group">
              {LANGUAGE_OPTIONS.map((o) => (
                <RadioCard
                  key={o.label}
                  checked={
                    JSON.stringify(o.hints) === JSON.stringify(status.language_hints ?? [])
                  }
                  onChange={() => void apply({ language_hints: o.hints })}
                  title={o.label}
                  desc={o.desc}
                />
              ))}
            </div>
          </section>

          <section className="voice-settings-section">
            <h3>可用性</h3>
            {status.available ? (
              <p
                style={{
                  color: 'var(--accent-green, #22c55e)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                }}
              >
                <CheckCircle2 size={15} /> 就绪：引擎「{status.provider === 'local' ? '本地 SenseVoice' : status.provider}」可用，
                输入框麦克风已是流式模式。
              </p>
            ) : (
              <p style={{ color: '#f59e0b', display: 'flex', alignItems: 'center', gap: 6 }}>
                <AlertCircle size={15} /> {status.reason} —— 云端档去「模型服务」给 DashScope 配
                API Key；本地档确认上方模型文件就绪。
              </p>
            )}
          </section>
        </>
      ) : null}

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 8 }}>
        {saving && <Loader2 size={14} className="spin" />}
        {saveOk && !saving && (
          <span className="search-save-toast ok">
            <CheckCircle2 size={14} /> 已保存并立即生效
          </span>
        )}
        {saveErr && (
          <span className="search-save-toast err">
            <AlertCircle size={14} /> 保存失败：{saveErr}
          </span>
        )}
      </div>
    </div>
  )
}
