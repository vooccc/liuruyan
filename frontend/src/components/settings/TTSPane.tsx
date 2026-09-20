/**
 * 设置 → 辅助功能 → 语音输出 子标签。
 *
 * 跟 VoiceTranscriptionPane（语音转写）对称：
 *   - 顶部 hero 描述用途
 *   - 总开关 + 三档朗读策略
 *   - provider 字段（base_url / api_key / model / voice / response_format / speed）
 *   - 试听区域：自定义文本 + 即时听效果
 *   - footer 保存状态
 *
 * 零污染契约：
 *   - 本组件独立，可单独 rm 删除
 *   - 复用现有 voice-settings-* CSS 类，避免引入新样式表
 *   - 不引用任何 chat / agent 主流模块
 *
 * 设计文档：_docs/handover_tts_design_2026-05-29.md
 */
import { ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  Copy,
  Loader2,
  PlayCircle,
  StopCircle,
  Volume2,
} from 'lucide-react'

import {
  getTTSConfig,
  TTSConfigResponse,
  TTSConfigUpdate,
  TTSConfigView,
  TTSEligibleProvider,
  updateTTSConfig,
  testTTS,
  blobToAudioURL,
} from '../../api/tts'
import {
  invalidateTtsStreamingConfig,
  resetTtsStreaming,
  subscribeSynthCount,
  getAuditionBlobs,
} from '../ttsStreamingSpeaker'
import { ttsAutoPlayer } from '../ttsAutoPlayer'

const DEFAULT_TRY_TEXT = '你好，老板。这里是语音输出测试，听到了吗？'

export default function TTSPane() {
  const [data, setData] = useState<TTSConfigResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)

  // 试听相关状态
  const [tryText, setTryText] = useState(DEFAULT_TRY_TEXT)
  const [testing, setTesting] = useState(false)
  const [testErr, setTestErr] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const lastUrlRef = useRef<string | null>(null)

  // system prompt snippet 复制反馈
  const [copyOk, setCopyOk] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    setLoadErr(null)
    try {
      const result = await getTTSConfig()
      setData(result)
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const [playerState, setPlayerState] = useState<'idle' | 'loading' | 'playing'>('idle')
  const [activeSynth, setActiveSynth] = useState(0)
  const auditioningRef = useRef(false)

  useEffect(() => {
    return ttsAutoPlayer.subscribe((state) => {
      setPlayerState(state)
    })
  }, [])

  useEffect(() => {
    return subscribeSynthCount((count) => {
      setActiveSynth(count)
    })
  }, [])

  useEffect(() => {
    return () => {
      if (lastUrlRef.current) {
        URL.revokeObjectURL(lastUrlRef.current)
      }
    }
  }, [])

  // 当后台流式合成与播放结束时，自动将收集到的片段合并为一个整段音频，加载到始终显示的 `<audio>` 控件中，供用户自由快进拖拽下载
  useEffect(() => {
    const isWorking = playerState !== 'idle' || activeSynth > 0
    if (!isWorking && auditioningRef.current) {
      auditioningRef.current = false
      const blobs = getAuditionBlobs()
      if (blobs.length > 0) {
        const mergedBlob = new Blob(blobs, { type: blobs[0].type })
        const url = URL.createObjectURL(mergedBlob)
        if (lastUrlRef.current) {
          URL.revokeObjectURL(lastUrlRef.current)
        }
        lastUrlRef.current = url
        if (audioRef.current) {
          audioRef.current.src = url
        }
      }
    }
  }, [playerState, activeSynth])

  const updateConfig = useCallback(
    async (patch: TTSConfigUpdate) => {
      if (!data) return
      setSaving(true)
      setSaveErr(null)
      try {
        const result = await updateTTSConfig(patch)
        setData((prev) => (prev ? { ...prev, config: result.config } : prev))
        invalidateTtsStreamingConfig()
      } catch (e) {
        setSaveErr(e instanceof Error ? e.message : String(e))
      } finally {
        setSaving(false)
      }
    },
    [data],
  )

  const handleTest = useCallback(async () => {
    if (!data) return

    // 一键叫停：如果正在后台合成，或者前台正在播放片段，点击按钮则立即中止
    if (playerState !== 'idle' || activeSynth > 0) {
      auditioningRef.current = false
      resetTtsStreaming('audition')
      if (lastUrlRef.current) {
        URL.revokeObjectURL(lastUrlRef.current)
        lastUrlRef.current = null
      }
      if (audioRef.current) {
        audioRef.current.src = ''
      }
      return
    }

    setTesting(true)
    setTestErr(null)
    if (lastUrlRef.current) {
      URL.revokeObjectURL(lastUrlRef.current)
      lastUrlRef.current = null
    }
    if (audioRef.current) {
      audioRef.current.src = ''
    }

    try {
      const blob = await testTTS(tryText)
      if (blob) {
        const url = blobToAudioURL(blob)
        lastUrlRef.current = url
        if (audioRef.current) {
          audioRef.current.src = url
          audioRef.current.play().catch(() => {})
        }
      }
    } catch (e) {
      setTestErr(e instanceof Error ? e.message : String(e))
    } finally {
      setTesting(false)
    }
  }, [data, tryText, playerState, activeSynth])

  const handleCopySnippet = useCallback(async () => {
    if (!data) return
    try {
      await navigator.clipboard.writeText(data.system_prompt_snippet)
      setCopyOk(true)
      setTimeout(() => setCopyOk(false), 1500)
    } catch {
      /* clipboard 不可用就静默 */
    }
  }, [data])

  if (loading) {
    return (
      <div style={{ padding: 24 }}>
        <Loader2 size={16} className="spin" /> 加载 TTS 配置…
      </div>
    )
  }

  if (loadErr || !data) {
    return (
      <div style={{ padding: 24, color: 'var(--accent-red)' }}>
        <AlertCircle size={16} /> 加载失败：{loadErr ?? '未知错误'}
        <button
          type="button"
          className="toolbar-action-btn"
          onClick={refresh}
          style={{ marginLeft: 12 }}
        >
          重试
        </button>
      </div>
    )
  }

  const { config, system_prompt_snippet } = data

  return (
    <div className="voice-settings">
      <header className="voice-settings-hero">
        <div className="voice-settings-hero-icon">
          <Volume2 size={20} />
        </div>
        <div className="voice-settings-hero-text">
          <h2>语音输出（TTS）</h2>
          <p>
            让 LLM 自己用{' '}
            <code>&lt;say&gt;...&lt;/say&gt;</code> 标记想语音输出的关键句<br />
            OpenAI 兼容端点（<code>/v1/audio/speech</code>）。一个 client 通吃 OpenAI /
            SiliconFlow / Groq / ElevenLabs / 社区 edge-tts wrapper。
          </p>
        </div>
      </header>

      {/* ── 总开关 + 朗读策略 ─────────────────────────────────────── */}
      <section className="voice-settings-section">
        <h3>总开关</h3>
        <label
          style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}
        >
          <input
            type="checkbox"
            checked={config.enabled}
            onChange={(e) => updateConfig({ enabled: e.target.checked })}
          />
          <span>
            启用 TTS（关闭时聊天界面不会订阅 <code>&lt;say&gt;</code>，但试听按钮依然可用）
          </span>
        </label>
      </section>

      <section className="voice-settings-section">
        <h3>朗读策略</h3>
        <div className="voice-radio-group">
          <RadioCard
            checked={config.mode === 'off'}
            onChange={() => updateConfig({ mode: 'off' })}
            title="关闭朗读（默认）"
            desc="完全不念。适合纯文字阅读场景。"
          />
          <RadioCard
            checked={config.mode === 'key'}
            onChange={() => updateConfig({ mode: 'key' })}
            title="关键句模式（推荐工作场景）"
            desc={'只念 <say> 标记的句子。LLM 没标且 ≤80 字短回复也念（如「好的」「完成了」）。代码 / 表格 / 长文不念。'}
          />
          <RadioCard
            checked={config.mode === 'all'}
            onChange={() => updateConfig({ mode: 'all' })}
            title="全文朗读（陪伴 / 挂机）"
            desc="念整段，包括代码长文。适合做别的事时让 AI 在旁边念给你听。"
          />
        </div>
      </section>

      {/* ── system prompt 片段（复制） ───────────────────────────── */}
      <section className="voice-settings-section">
        <h3>
          <span>system prompt 片段</span>
          <button
            type="button"
            className="toolbar-action-btn"
            onClick={handleCopySnippet}
            style={{ marginLeft: 12, fontSize: 12 }}
          >
            {copyOk ? <CheckCircle2 size={13} /> : <Copy size={13} />}
            <span style={{ marginLeft: 4 }}>{copyOk ? '已复制' : '复制'}</span>
          </button>
        </h3>
        <p style={{ fontSize: 13, color: 'var(--text-muted)' }}>
          复制粘贴到角色 system prompt 末尾，让 LLM 学会用 <code>&lt;say&gt;</code> 标记。
          <strong>不粘贴也能跑</strong>——兜底规则会让 ≤80 字的短回复在 key 模式下念出来。
        </p>
        <pre
          style={{
            background: 'var(--bg-secondary)',
            padding: 12,
            borderRadius: 6,
            fontSize: 12,
            whiteSpace: 'pre-wrap',
            margin: 0,
          }}
        >
          {system_prompt_snippet}
        </pre>
      </section>

      {/* ── 流式朗读设置 ────────────────────────────────────────── */}
      <section className="voice-settings-section">
        <h3>流式朗读设置</h3>
        <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 12 }}>
          聊天回复边生成边朗读的参数，降低首字播放延迟。
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={config.streaming_enabled}
              onChange={(e) => updateConfig({ streaming_enabled: e.target.checked })}
            />
            <span style={{ fontSize: 13 }}>启用流式朗读（按句切片提前合成，关闭时回退到整段完成后合成）</span>
          </label>

          {config.streaming_enabled && (
            <div style={{ paddingLeft: 20, display: 'flex', flexDirection: 'column', gap: 14, borderLeft: '2px solid var(--border-muted)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <span style={{ fontSize: 13, minWidth: 100 }}>最小切片字数:</span>
                <input
                  type="number"
                  value={config.streaming_min_segment_chars}
                  onChange={(e) => updateConfig({ streaming_min_segment_chars: parseInt(e.target.value) || 1 })}
                  style={{ width: 80, padding: '4px 8px', fontSize: 13, background: 'var(--bg-secondary)', border: '1px solid var(--border-muted)', borderRadius: 4, color: 'var(--text-primary)' }}
                  min={1}
                  max={500}
                />
                <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                  避免过于细碎的文本（如标点或单个语气词）频繁打扰或导致播放过于卡顿。
                </span>
              </div>

              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <span style={{ fontSize: 13, minWidth: 100 }}>切句标点符号:</span>
                <input
                  type="text"
                  value={config.streaming_sentence_enders}
                  onChange={(e) => updateConfig({ streaming_sentence_enders: e.target.value })}
                  style={{ width: 180, padding: '4px 8px', fontSize: 13, background: 'var(--bg-secondary)', border: '1px solid var(--border-muted)', borderRadius: 4, color: 'var(--text-primary)' }}
                  placeholder="例如 .!?。！？\n"
                />
                <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                  识别到这些字符时认为是一句的结束。
                </span>
              </div>

              <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={config.streaming_protect_say_tags}
                  onChange={(e) => updateConfig({ streaming_protect_say_tags: e.target.checked })}
                />
                <span style={{ fontSize: 13 }}>保护 <code>&lt;say&gt;</code> 标签（不对内部标点切片，防止标签被剪断导致后端无法正确识别和提取）</span>
              </label>

              <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={config.streaming_flush_tail_on_complete}
                  onChange={(e) => updateConfig({ streaming_flush_tail_on_complete: e.target.checked })}
                />
                <span style={{ fontSize: 13 }}>
                  合成末尾残余文本（流结束后，如果最后一句话没有遇到切句标点，也强制将其合成并播放出来）
                </span>
              </label>
            </div>
          )}
        </div>
      </section>

      {/* ── 后端协议切换 ────────────────────────────────────────── */}
      <section className="voice-settings-section">
        <h3>后端协议</h3>
        <div className="voice-radio-group">
          <RadioCard
            checked={config.provider_type === 'openai_compat'}
            onChange={() => updateConfig({ provider_type: 'openai_compat' })}
            title="OpenAI 兼容（云端 / 本地 wrapper）"
            desc="对接 SiliconFlow / OpenAI / Groq / edge-tts wrapper 等支持 /v1/audio/speech 的服务"
          />
          <RadioCard
            checked={config.provider_type === 'gptsovits_compat'}
            onChange={() => updateConfig({ provider_type: 'gptsovits_compat' })}
            title="GPT-SoVITS / Qwen3TTS（本地）"
            desc="对接你本地跑的 GPT-SoVITS 或 Qwen3TTS-API 服务（POST 根路径，需参考音频）"
          />
          <RadioCard
            checked={config.provider_type === 'tencent_compat'}
            onChange={() => updateConfig({ provider_type: 'tencent_compat' })}
            title="腾讯云语音合成"
            desc="对接腾讯云 TTS API（TextToVoice），需 SecretId 和 SecretKey"
          />
          <RadioCard
            checked={config.provider_type === 'baidu_compat'}
            onChange={() => updateConfig({ provider_type: 'baidu_compat' })}
            title="百度云语音合成"
            desc="对接百度智能云短文本语音合成，需 API Key 和 Secret Key"
          />
          <RadioCard
            checked={config.provider_type === 'aliyun_compat'}
            onChange={() => updateConfig({ provider_type: 'aliyun_compat' })}
            title="阿里云语音合成"
            desc="对接阿里云智能语音交互 NLS，需 AccessKey 和 AppKey"
          />
        </div>
      </section>

      {/* ── Provider 配置区：按 provider_type 切换面板 ─────────────── */}
      {config.provider_type === 'openai_compat' ? (
        <ProviderPanel
          config={config}
          eligibleProviders={data.eligible_providers}
          recommendedModels={data.recommended_models}
          recommendedVoices={data.recommended_voices}
          onUpdate={updateConfig}
        />
      ) : config.provider_type === 'tencent_compat' ? (
        <TencentPanel config={config} onUpdate={updateConfig} />
      ) : config.provider_type === 'baidu_compat' ? (
        <BaiduPanel config={config} voices={data.baidu_voices} onUpdate={updateConfig} />
      ) : config.provider_type === 'aliyun_compat' ? (
        <AliyunPanel
          config={config}
          voices={data.aliyun_voices}
          emotions={data.aliyun_emotions}
          onUpdate={updateConfig}
        />
      ) : (
        <GPTSoVITSPanel config={config} onUpdate={updateConfig} />
      )}

      {/* ── 试听 ─────────────────────────────────────────────────── */}
      <section className="voice-settings-section">
        <h3>试听</h3>
        <p className="voice-settings-hint">
          试听<strong>不过</strong> <code>&lt;say&gt;</code> 过滤，直接字面合成。凭证从「模型服务」里选中的 Provider 借用。
        </p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <textarea
            value={tryText}
            onChange={(e) => setTryText(e.target.value)}
            placeholder="输入试听文本（≤500 字符）"
            maxLength={500}
            rows={2}
            style={{ flex: 1, fontSize: 13, padding: 8, background: 'var(--bg-secondary)', border: '1px solid var(--border-muted)', borderRadius: 4, color: 'var(--text-primary)' }}
          />
          <button
            type="button"
            className="toolbar-action-btn"
            onClick={handleTest}
            disabled={!tryText.trim()}
            style={{ minWidth: 90 }}
          >
            {playerState !== 'idle' || activeSynth > 0 ? (
              <>
                <StopCircle size={14} style={{ color: 'var(--accent-red)' }} />
                <span>停止</span>
              </>
            ) : testing ? (
              <>
                <Loader2 size={14} className="spin" />
                <span>切片中…</span>
              </>
            ) : (
              <>
                <PlayCircle size={14} />
                <span>试听</span>
              </>
            )}
          </button>
        </div>
        <audio ref={audioRef} controls style={{ marginTop: 12, width: '100%', borderRadius: 4, background: 'var(--bg-secondary)', outline: 'none' }} />
        {testErr && (
          <div style={{ color: 'var(--accent-red)', marginTop: 8, fontSize: 13 }}>
            <AlertCircle size={13} /> {testErr}
          </div>
        )}
      </section>

      {/* ── footer：保存状态 ─────────────────────────────────────── */}
      <footer className="voice-settings-footer">
        {saving ? (
          <span className="voice-settings-status">
            <Loader2 size={13} className="spin" /> 保存中…
          </span>
        ) : saveErr ? (
          <span className="voice-settings-status err">
            <AlertCircle size={13} /> 保存失败：{saveErr}
          </span>
        ) : (
          <span className="voice-settings-status ok">
            <CheckCircle2 size={13} /> 已与服务端同步（修改即时生效）
          </span>
        )}
      </footer>
    </div>
  )
}

/* ── 内部子组件 ─────────────────────────────────────────────── */

/**
 * 字段排版：左侧小标题 + 右侧表单控件 + 可选 hint。
 * 复用 voice-settings-* 既有 CSS，避免引入新样式表。
 */
function Field({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 8 }}>
      <span style={{ fontSize: 12.5, fontWeight: 500, color: 'var(--text-secondary)' }}>
        {label}
      </span>
      {children}
    </div>
  )
}

/**
 * Provider 选择 + Model 选择 + Voice 选择 + 音频格式 + 语速。
 *
 * 跟 VoiceSettings 的 ApiPanel 同思路：从「模型服务」（providers store）借列表，
 * 不再让用户在 TTSPane 里独立填 API Key —— 凭证去模型服务那边管。
 *
 * Model 字段：优先用所选 provider 的 models[] 做下拉；用户也可以切换到"手输"
 * 兜底（provider 还没拉模型列表时必备）。
 * Voice 字段：手输 + 推荐 voice 数组生成 hint。
 */
function ProviderPanel({
  config,
  eligibleProviders,
  recommendedModels,
  recommendedVoices,
  onUpdate,
}: {
  config: TTSConfigView
  eligibleProviders: TTSEligibleProvider[]
  recommendedModels: Record<string, string>
  recommendedVoices: Record<string, string[]>
  onUpdate: (patch: TTSConfigUpdate) => void
}) {
  // 找到当前选中的 provider（可能为 undefined：未选 / 选了但被删除）
  const selectedProvider = eligibleProviders.find((p) => p.id === config.provider_id)

  // 当前 provider 的 model 列表（Combobox 下拉提示用）
  const modelOptions = useMemo(
    () =>
      (selectedProvider?.models ?? []).map((m) => ({
        value: m.id,
        label: m.name && m.name !== m.id ? `${m.name} · ${m.id}` : m.id,
      })),
    [selectedProvider],
  )
  const hasModels = modelOptions.length > 0

  // 切换 provider 时一键填推荐 model（如果用户当前 model 为空）
  const handleProviderChange = (pid: string) => {
    const patch: TTSConfigUpdate = { provider_id: pid }
    const recommended = recommendedModels[pid]
    if (!config.model && recommended) {
      patch.model = recommended
    }
    onUpdate(patch)
  }

  // 当前 provider 的推荐 voice 列表（Combobox 下拉提示用）
  const voiceSuggestions = config.provider_id ? recommendedVoices[config.provider_id] ?? [] : []
  const hasVoiceSuggestions = voiceSuggestions.length > 0

  return (
    <section className="voice-settings-section">
      <h3>Provider（来自「模型服务」）</h3>

      {eligibleProviders.length === 0 ? (
        <div className="voice-settings-warn">
          <AlertCircle size={14} />
          没有可用的 OpenAI 兼容 Provider。请先到「设置 → 模型服务」启用一个
          provider（如 OpenAI / SiliconFlow / Groq），或添加自定义 Provider
          指向本地 server（如 <code>http://127.0.0.1:5050/v1</code> 接 openai-edge-tts，
          或本地 OpenAI 兼容的 Qwen3-TTS / GPT-SoVITS wrapper）。
        </div>
      ) : (
        <Field label="Provider">
          <select
            className="voice-select"
            value={config.provider_id}
            onChange={(e) => handleProviderChange(e.target.value)}
          >
            <option value="">请选择 Provider…</option>
            {eligibleProviders.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
                {p.is_aggregator ? ' · 聚合站' : ''}
                {p.has_api_key ? '' : ' · 未配 Key'}
              </option>
            ))}
          </select>
          {selectedProvider && !selectedProvider.has_api_key && (
            <p className="voice-settings-hint" style={{ color: 'var(--accent-red)' }}>
              ⚠ 这个 Provider 还没配 API Key。请到「设置 → 模型服务 → {selectedProvider.name}」填好 Key 再回来试听。
            </p>
          )}
          {selectedProvider?.has_api_key && (
            <p className="voice-settings-hint">
              ✓ 凭证已就绪（base_url: <code>{selectedProvider.base_url || '未填'}</code>）。
            </p>
          )}
        </Field>
      )}

      {/* Model：一体化 Combobox —— 鼠标点输入框能手输，点右侧箭头能弹下拉 */}
      <Field label="Model">
        <Combobox
          value={config.model}
          options={modelOptions}
          placeholder={
            selectedProvider
              ? `推荐：${recommendedModels[selectedProvider.id] || 'tts-1'}`
              : 'tts-1 / FunAudioLLM/CosyVoice2-0.5B'
          }
          onChange={(v) => onUpdate({ model: v })}
          ariaLabel="TTS 模型 ID"
        />
        {!hasModels && selectedProvider && (
          <p className="voice-settings-hint">
            该 Provider 还没有任何已配置模型。可以到「模型服务」拉取，或在这里直接手输 TTS 模型名。
          </p>
        )}
      </Field>

      {/* Voice：一体化 Combobox —— 鼠标点输入框能手输，点右侧箭头能弹下拉 */}
      <Field label="Voice">
        <Combobox
          value={config.voice}
          options={voiceSuggestions.map((v) => ({ value: v, label: v }))}
          placeholder={
            hasVoiceSuggestions
              ? `推荐：${voiceSuggestions[0]}`
              : 'alloy / nova / zh-CN-XiaoxiaoNeural'
          }
          onChange={(v) => onUpdate({ voice: v })}
          ariaLabel="音色 ID"
        />
        {!hasVoiceSuggestions && selectedProvider && (
          <p className="voice-settings-hint">
            该 Provider 没有内置推荐音色，请手动输入合法的 voice ID（参考 Provider 文档）。
          </p>
        )}
      </Field>

      {/* 音频格式 */}
      <Field label="音频格式">
        <select
          className="voice-select"
          value={config.response_format}
          onChange={(e) =>
            onUpdate({ response_format: e.target.value as TTSConfigView['response_format'] })
          }
        >
          <option value="mp3">mp3（推荐，兼容性最好）</option>
          <option value="wav">wav（无损，体积大）</option>
          <option value="opus">opus（最小，需新浏览器）</option>
          <option value="pcm">pcm（原始，几乎不用）</option>
        </select>
      </Field>

      {/* 语速 */}
      <Field label={`语速（${config.speed.toFixed(2)}×）`}>
        <input
          type="range"
          min={0.5}
          max={2}
          step={0.05}
          value={config.speed}
          onChange={(e) => onUpdate({ speed: Number(e.target.value) })}
          style={{ width: '100%' }}
        />
      </Field>
    </section>
  )
}

/**
 * GPT-SoVITS / Qwen3TTS-API 配置面板。
 *
 * 跟 ProviderPanel 的差异：
 * - 不复用 providers store（本地服务无认证概念）
 * - 必填 refer_wav_path（zero-shot 风格的参考音频）
 * - 文本语言是 ISO 简码（zh/ja/en），后端会映射成 GPT-SoVITS 的全名（Chinese/Japanese/English）
 *
 * 用户路径：本地起 run-1.7b.bat（默认 9880 端口），把参考音频放到本地某路径，
 * 在这里填好就能跑。
 */
function GPTSoVITSPanel({
  config,
  onUpdate,
}: {
  config: TTSConfigView
  onUpdate: (patch: TTSConfigUpdate) => void
}) {
  return (
    <section className="voice-settings-section">
      <h3>GPT-SoVITS / Qwen3TTS 配置</h3>
      <p className="voice-settings-hint">
        本地服务参数。先确认你本地的 Qwen3TTS / GPT-SoVITS 服务已启动（如运行
        <code> run-1.7b.bat </code>，默认监听 <code>http://127.0.0.1:9880</code>）。
        参考音频要用<strong>绝对路径</strong>。
      </p>

      <Field label="服务地址">
        <input
          className="voice-input"
          type="text"
          value={config.gptsovits_base_url}
          placeholder="http://127.0.0.1:9880"
          onChange={(e) => onUpdate({ gptsovits_base_url: e.target.value })}
        />
      </Field>

      <Field label="参考音频路径">
        <input
          className="voice-input"
          type="text"
          value={config.gptsovits_refer_wav_path}
          placeholder="D:\audio\reference.mp3"
          onChange={(e) =>
            onUpdate({ gptsovits_refer_wav_path: e.target.value })
          }
        />
        <p className="voice-settings-hint">
          GPT-SoVITS 是 zero-shot 风格——给一段 5-10 秒的参考音频，模型克隆这个音色。
          支持 mp3 / wav / flac / ogg / m4a。
        </p>
      </Field>

      <Field label="参考音频文字稿（可选）">
        <input
          className="voice-input"
          type="text"
          value={config.gptsovits_prompt_text}
          placeholder="参考音频里说的话（中文通常可以省略）"
          onChange={(e) => onUpdate({ gptsovits_prompt_text: e.target.value })}
        />
        <p className="voice-settings-hint">
          日文 / 英文参考音频<strong>建议填</strong>对应文字稿，能显著提升音色相似度。
          中文参考音频通常省略也能跑。
        </p>
      </Field>

      <Field label="合成文本语言">
        <Combobox
          value={config.gptsovits_text_lang}
          options={[
            { value: 'zh', label: 'zh · 中文' },
            { value: 'ja', label: 'ja · 日文' },
            { value: 'en', label: 'en · 英文' },
          ]}
          placeholder="zh / ja / en"
          onChange={(v) => onUpdate({ gptsovits_text_lang: v })}
          ariaLabel="合成文本语言"
        />
      </Field>
    </section>
  )
}

/* ── 百度云 TTS 配置面板 ──────────────────────────────────── */

function BaiduPanel({
  config,
  voices,
  onUpdate,
}: {
  config: TTSConfigView
  voices?: { per: number; name: string; style: string; tags: string[] }[]
  onUpdate: (patch: TTSConfigUpdate) => void
}) {
  const [skDirty, setSkDirty] = useState<string | null>(null)

  return (
    <section className="voice-settings-section">
      <h3>百度云语音合成配置</h3>
      <p className="voice-settings-hint">
        对接百度智能云短文本语音合成。需在百度云控制台创建语音技术应用，获取
        API Key 和 Secret Key，并确认应用已开通语音合成能力。
      </p>

      <Field label="API Key">
        <input
          className="voice-input"
          type="text"
          value={config.baidu_api_key}
          placeholder="百度云应用 API Key"
          onChange={(e) => onUpdate({ baidu_api_key: e.target.value })}
        />
      </Field>

      <Field label="Secret Key">
        <input
          className="voice-input"
          type="password"
          value={
            skDirty !== null
              ? skDirty
              : config.has_baidu_secret_key
              ? '••••••已配置'
              : ''
          }
          placeholder="点击填写 Secret Key"
          onFocus={() => {
            if (config.has_baidu_secret_key && skDirty === null) {
              setSkDirty('')
            }
          }}
          onChange={(e) => setSkDirty(e.target.value)}
          onBlur={() => {
            if (skDirty !== null) {
              onUpdate({ baidu_secret_key: skDirty })
              setSkDirty(null)
            }
          }}
        />
        {config.has_baidu_secret_key ? (
          <p className="voice-settings-hint">✓ Secret Key 已配置</p>
        ) : (
          <p className="voice-settings-hint" style={{ color: 'var(--accent-red)' }}>
            ⚠ Secret Key 未配置，合成将失败
          </p>
        )}
      </Field>

      <Field label="发音人">
        <Combobox
          value={String(config.baidu_voice)}
          options={(voices || []).map((voice) => ({
            value: String(voice.per),
            label: `${voice.name} · ${voice.style} · per=${voice.per}`,
          }))}
          placeholder="搜索音色名，或直接输入 per 编号"
          onChange={(v) => {
            const n = Number(v.trim())
            if (Number.isFinite(n)) {
              onUpdate({ baidu_voice: n })
            }
          }}
          ariaLabel="百度云发音人"
        />
        <p className="voice-settings-hint">
          下拉列出常见百度音色；如果你的账号开通了更多精品音色，可直接输入文档里的 per 编号试听。
        </p>
      </Field>

      <Field label="客户端标识 CUID">
        <input
          className="voice-input"
          type="text"
          value={config.baidu_cuid}
          placeholder="liuruyan"
          onChange={(e) => onUpdate({ baidu_cuid: e.target.value })}
        />
        <p className="voice-settings-hint">任意字符串，用于百度侧区分客户端；默认 liuruyan 即可。</p>
      </Field>

      <Field label={`语速（${config.baidu_speed}）`}>
        <input
          type="range"
          min={0}
          max={15}
          step={1}
          value={config.baidu_speed}
          onChange={(e) => onUpdate({ baidu_speed: Number(e.target.value) })}
          style={{ width: '100%' }}
        />
      </Field>

      <Field label={`音调（${config.baidu_pitch}）`}>
        <input
          type="range"
          min={0}
          max={15}
          step={1}
          value={config.baidu_pitch}
          onChange={(e) => onUpdate({ baidu_pitch: Number(e.target.value) })}
          style={{ width: '100%' }}
        />
      </Field>

      <Field label={`音量（${config.baidu_volume}）`}>
        <input
          type="range"
          min={0}
          max={15}
          step={1}
          value={config.baidu_volume}
          onChange={(e) => onUpdate({ baidu_volume: Number(e.target.value) })}
          style={{ width: '100%' }}
        />
      </Field>

      <Field label="音频格式">
        <select
          className="voice-select"
          value={config.response_format}
          onChange={(e) =>
            onUpdate({ response_format: e.target.value as TTSConfigView['response_format'] })
          }
        >
          <option value="mp3">mp3（推荐）</option>
          <option value="wav">wav（无损）</option>
          <option value="pcm">pcm（原始，极少用）</option>
        </select>
        <p className="voice-settings-hint">百度云不支持 opus 格式，选了会自动降级为 mp3。</p>
      </Field>
    </section>
  )
}

/* ── 阿里云 TTS 配置面板 ──────────────────────────────────── */

function AliyunPanel({
  config,
  voices,
  emotions,
  onUpdate,
}: {
  config: TTSConfigView
  voices?: { voice: string; name: string; style: string; tags: string[] }[]
  emotions?: { value: string; label: string }[]
  onUpdate: (patch: TTSConfigUpdate) => void
}) {
  const [secretDirty, setSecretDirty] = useState<string | null>(null)

  return (
    <section className="voice-settings-section">
      <h3>阿里云语音合成配置</h3>
      <p className="voice-settings-hint">
        对接阿里云智能语音交互 NLS。需要 RAM AccessKey、AccessKey Secret，以及语音交互项目的 AppKey。
      </p>

      <Field label="AccessKey ID">
        <input
          className="voice-input"
          type="text"
          value={config.aliyun_access_key_id}
          placeholder="LTAIxxxxxxxxxxxxxxxx"
          onChange={(e) => onUpdate({ aliyun_access_key_id: e.target.value })}
        />
      </Field>

      <Field label="AccessKey Secret">
        <input
          className="voice-input"
          type="password"
          value={
            secretDirty !== null
              ? secretDirty
              : config.has_aliyun_access_key_secret
              ? '••••••已配置'
              : ''
          }
          placeholder="点击填写 AccessKey Secret"
          onFocus={() => {
            if (config.has_aliyun_access_key_secret && secretDirty === null) {
              setSecretDirty('')
            }
          }}
          onChange={(e) => setSecretDirty(e.target.value)}
          onBlur={() => {
            if (secretDirty !== null) {
              onUpdate({ aliyun_access_key_secret: secretDirty })
              setSecretDirty(null)
            }
          }}
        />
        {config.has_aliyun_access_key_secret ? (
          <p className="voice-settings-hint">✓ AccessKey Secret 已配置</p>
        ) : (
          <p className="voice-settings-hint" style={{ color: 'var(--accent-red)' }}>
            ⚠ AccessKey Secret 未配置，合成将失败
          </p>
        )}
      </Field>

      <Field label="AppKey">
        <input
          className="voice-input"
          type="text"
          value={config.aliyun_appkey}
          placeholder="阿里云智能语音交互项目 AppKey"
          onChange={(e) => onUpdate({ aliyun_appkey: e.target.value })}
        />
      </Field>

      <Field label="地域">
        <select
          className="voice-select"
          value={config.aliyun_region}
          onChange={(e) => onUpdate({ aliyun_region: e.target.value })}
        >
          <option value="cn-shanghai">cn-shanghai（上海，默认）</option>
          <option value="cn-beijing">cn-beijing（北京）</option>
        </select>
      </Field>

      <Field label="音色 voice">
        <Combobox
          value={config.aliyun_voice}
          options={(voices || []).map((voice) => ({
            value: voice.voice,
            label: `${voice.name} · ${voice.style} · ${voice.voice}`,
          }))}
          placeholder="搜索人物/音色，或手填控制台可用 voice"
          onChange={(v) => onUpdate({ aliyun_voice: v })}
          ariaLabel="阿里云音色"
        />
        <p className="voice-settings-hint">下拉列出常见人物/音色；如果控制台里有新增音色，可直接手填它的 voice 参数名。</p>
      </Field>

      <Field label="情感 emotion">
        <Combobox
          value={config.aliyun_emotion}
          options={emotions || []}
          placeholder="选择情感，或手填阿里云支持的 emotion 值"
          onChange={(v) => onUpdate({ aliyun_emotion: v })}
          ariaLabel="阿里云情感"
        />
        <p className="voice-settings-hint">多情感音色才会生效；普通音色可保持“不指定”。</p>
      </Field>

      <Field label="采样率">
        <select
          className="voice-select"
          value={config.aliyun_sample_rate}
          onChange={(e) => onUpdate({ aliyun_sample_rate: Number(e.target.value) })}
        >
          <option value={16000}>16000（推荐）</option>
          <option value={24000}>24000（高音质）</option>
          <option value={8000}>8000（电话质量）</option>
        </select>
      </Field>

      <Field label={`音量（${config.aliyun_volume}）`}>
        <input
          type="range"
          min={0}
          max={100}
          step={1}
          value={config.aliyun_volume}
          onChange={(e) => onUpdate({ aliyun_volume: Number(e.target.value) })}
          style={{ width: '100%' }}
        />
      </Field>

      <Field label={`语速（${config.aliyun_speech_rate}）`}>
        <input
          type="range"
          min={-500}
          max={500}
          step={10}
          value={config.aliyun_speech_rate}
          onChange={(e) => onUpdate({ aliyun_speech_rate: Number(e.target.value) })}
          style={{ width: '100%' }}
        />
      </Field>

      <Field label={`语调（${config.aliyun_pitch_rate}）`}>
        <input
          type="range"
          min={-500}
          max={500}
          step={10}
          value={config.aliyun_pitch_rate}
          onChange={(e) => onUpdate({ aliyun_pitch_rate: Number(e.target.value) })}
          style={{ width: '100%' }}
        />
      </Field>

      <Field label="音频格式">
        <select
          className="voice-select"
          value={config.response_format}
          onChange={(e) =>
            onUpdate({ response_format: e.target.value as TTSConfigView['response_format'] })
          }
        >
          <option value="mp3">mp3（推荐）</option>
          <option value="wav">wav</option>
          <option value="pcm">pcm</option>
          <option value="opus">opus</option>
        </select>
      </Field>
    </section>
  )
}

/* ── 腾讯云 TTS 配置面板 ──────────────────────────────────── */

function TencentPanel({
  config,
  onUpdate,
}: {
  config: TTSConfigView
  onUpdate: (patch: TTSConfigUpdate) => void
}) {
  // SecretKey 后端只返 has_tencent_secret_key 布尔不返真值，
  // 所以要用局部 state 让用户能自由输入，失焦时才保存。
  const [skDirty, setSkDirty] = useState<string | null>(null)

  return (
    <section className="voice-settings-section">
      <h3>腾讯云语音合成配置</h3>
      <p className="voice-settings-hint">
        对接腾讯云 TTS 服务。需在腾讯云控制台（访问管理 → API 密钥管理）获取
        SecretId 和 SecretKey，并到语音合成控制台开通服务。
      </p>

      <Field label="SecretId">
        <input
          className="voice-input"
          type="text"
          value={config.tencent_secret_id}
          placeholder="输入腾讯云 SecretId"
          onChange={(e) => onUpdate({ tencent_secret_id: e.target.value })}
        />
      </Field>

      <Field label="SecretKey">
        <input
          className="voice-input"
          type="password"
          value={
            skDirty !== null
              ? skDirty
              : config.has_tencent_secret_key
              ? '••••••已配置'
              : ''
          }
          placeholder="点击填写 SecretKey"
          onFocus={() => {
            // 已配置时点进去清掉占位，让用户重新输入
            if (config.has_tencent_secret_key && skDirty === null) {
              setSkDirty('')
            }
          }}
          onChange={(e) => setSkDirty(e.target.value)}
          onBlur={() => {
            if (skDirty !== null) {
              onUpdate({ tencent_secret_key: skDirty })
              setSkDirty(null) // 下次走 config.has_tencent_secret_key 展示
            }
          }}
        />
        {config.has_tencent_secret_key ? (
          <p className="voice-settings-hint">✓ SecretKey 已配置</p>
        ) : (
          <p className="voice-settings-hint" style={{ color: 'var(--accent-red)' }}>
            ⚠ SecretKey 未配置，合成将失败
          </p>
        )}
      </Field>

      <Field label="音色">
        <select
          className="voice-select"
          value={config.tencent_voice_type}
          onChange={(e) => onUpdate({ tencent_voice_type: Number(e.target.value) })}
        >
          <option value={101001}>101001 · 情感女声（温柔）</option>
          <option value={101002}>101002 · 情感女声（知性）</option>
          <option value={101003}>101003 · 情感女声（甜美）</option>
          <option value={101008}>101008 · 情感女声（怀念）</option>
          <option value={101009}>101009 · 情感女声（青涩）</option>
          <option value={101050}>101050 · 通用女声</option>
          <option value={101051}>101051 · 通用男声</option>
        </select>
      </Field>

      <Field label="地域">
        <select
          className="voice-select"
          value={config.tencent_region}
          onChange={(e) => onUpdate({ tencent_region: e.target.value })}
        >
          <option value="ap-guangzhou">ap-guangzhou（广州，默认）</option>
          <option value="ap-beijing">ap-beijing（北京）</option>
          <option value="ap-shanghai">ap-shanghai（上海）</option>
          <option value="ap-singapore">ap-singapore（新加坡）</option>
        </select>
      </Field>

      <Field label="音频格式">
        <select
          className="voice-select"
          value={config.response_format}
          onChange={(e) =>
            onUpdate({ response_format: e.target.value as TTSConfigView['response_format'] })
          }
        >
          <option value="mp3">mp3（推荐）</option>
          <option value="wav">wav（无损）</option>
          <option value="pcm">pcm（原始，极少用）</option>
        </select>
        <p className="voice-settings-hint">腾讯云不支持 opus 格式，选了会自动降级为 mp3。</p>
      </Field>
    </section>
  )
}

interface ComboboxOption {
  value: string
  label: ReactNode
}

/**
 * 一体化 combobox：input 永远能手输，右侧 chevron 按钮一点弹下拉。
 *
 * - 点 input：照常聚焦打字
 * - 点 chevron：开/关下拉浮层
 * - 输入文本：实时过滤推荐项（包含匹配，大小写不敏感）
 * - 点下拉项：填回 input + 关闭浮层
 * - 点外部 / Esc：关闭
 * - 没推荐项时不渲染 chevron，纯净退化为 input
 *
 * 跟 EmbeddingSettings 的 ProviderPicker 不同：那个是纯 select，
 * 不能手输；这里既允许选也允许填自定义 ID。
 */
function Combobox({
  value,
  options,
  placeholder,
  onChange,
  ariaLabel,
}: {
  value: string
  options: ComboboxOption[]
  placeholder?: string
  onChange: (v: string) => void
  ariaLabel?: string
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const hasOptions = options.length > 0

  // 点击外部关闭
  useEffect(() => {
    if (!open) return
    function onDown(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  // 输入文本时过滤推荐项（包含匹配）
  // 关键陷阱：value 精确等于某个 option 时（=用户已选定，不是在搜索），
  // 应展示全部，而不是只显示自己 —— 否则下拉会"只剩一个"。
  const filtered = useMemo(() => {
    const q = value.trim().toLowerCase()
    if (!q) return options
    const isExactMatch = options.some((o) => o.value.toLowerCase() === q)
    if (isExactMatch) return options
    return options.filter(
      (o) =>
        o.value.toLowerCase().includes(q) ||
        (typeof o.label === 'string' && o.label.toLowerCase().includes(q)),
    )
  }, [value, options])

  return (
    <div className="tts-combobox" ref={wrapRef}>
      <input
        ref={inputRef}
        className="tts-combobox-input"
        type="text"
        value={value}
        placeholder={placeholder}
        aria-label={ariaLabel}
        aria-autocomplete="list"
        aria-expanded={open}
        onChange={(e) => {
          onChange(e.target.value)
          if (hasOptions) setOpen(true)
        }}
        onFocus={() => {
          if (hasOptions) setOpen(true)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            setOpen(false)
          } else if (e.key === 'ArrowDown' && hasOptions && !open) {
            setOpen(true)
          }
        }}
      />
      {hasOptions && (
        <button
          type="button"
          className={`tts-combobox-chevron ${open ? 'open' : ''}`}
          onMouseDown={(e) => {
            // 防止点 chevron 时 input blur 抖动
            e.preventDefault()
            setOpen((v) => !v)
            inputRef.current?.focus()
          }}
          tabIndex={-1}
          aria-label="展开建议列表"
        >
          <ChevronDown size={14} />
        </button>
      )}
      {open && filtered.length > 0 && (
        <div className="tts-combobox-menu" role="listbox">
          {filtered.map((o) => (
            <button
              key={o.value}
              type="button"
              role="option"
              aria-selected={o.value === value}
              className={`tts-combobox-option ${o.value === value ? 'selected' : ''}`}
              onMouseDown={(e) => {
                // 阻止 input blur，防止 onClick 来不及触发
                e.preventDefault()
              }}
              onClick={() => {
                onChange(o.value)
                setOpen(false)
                inputRef.current?.focus()
              }}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

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
    <button
      type="button"
      className={`voice-radio-card ${checked ? 'checked' : ''}`}
      onClick={onChange}
    >
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
