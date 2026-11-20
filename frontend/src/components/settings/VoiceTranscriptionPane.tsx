/**
 * 设置 → 辅助功能 → 语音转写 子标签。
 *
 * 孤岛模块：
 * - 自包含 RadioCard / Combobox / Field，复用现有 voice-settings-* 与 tts-combobox* CSS；
 * - 每次修改立即 PUT /voice/transcription/config（文本字段采用草稿 + blur/Enter 提交）；
 * - 可随时删除本文件 + api/voiceTranscription.ts 并还原 VoiceSettings.tsx 接线。
 */
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  Copy,
  Download,
  Eye,
  EyeOff,
  FileAudio,
  Loader2,
  PlayCircle,
  Upload,
} from 'lucide-react'

import {
  getVoiceTranscriptionConfig,
  startTranscriptionModelDownload,
  transcribeAudioFile,
  updateVoiceTranscriptionConfig,
  type VoiceTranscriptionConfigResponse,
  type VoiceTranscriptionConfigUpdate,
  type VoiceTranscriptionResponseFormat,
  type VoiceTranscriptionResult,
} from '../../api/voiceTranscription'

const LANGUAGE_CHOICES = [
  { value: '', label: '自动检测（推荐，支持中英混说）' },
  { value: 'zh', label: '中文（zh）' },
  { value: 'en', label: '英文（en）' },
  { value: 'ja', label: '日文（ja）' },
  { value: 'yue', label: '粤语（yue）' },
  { value: 'ko', label: '韩文（ko）' },
]

function formatTime(sec?: number): string {
  if (sec === undefined || !Number.isFinite(sec)) return ''
  const m = Math.floor(sec / 60)
  const s = (sec % 60).toFixed(1).padStart(4, '0')
  return `${String(m).padStart(2, '0')}:${s}`
}

export default function VoiceTranscriptionPane() {
  const [data, setData] = useState<VoiceTranscriptionConfigResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)

  // 文本输入草稿（blur / Enter 时提交，避免逐字触发 PUT）
  const [baseUrlDraft, setBaseUrlDraft] = useState<string | null>(null)
  const [apiKeyDraft, setApiKeyDraft] = useState<string | null>(null)
  const [showApiKey, setShowApiKey] = useState(false)
  const [modelDraft, setModelDraft] = useState<string | null>(null)
  const [promptDraft, setPromptDraft] = useState<string | null>(null)
  const [localDirDraft, setLocalDirDraft] = useState<string | null>(null)
  const [dlReqErr, setDlReqErr] = useState<string | null>(null)

  // 测试转写区状态
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [testFile, setTestFile] = useState<File | null>(null)
  const [testTimestamps, setTestTimestamps] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testErr, setTestErr] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<VoiceTranscriptionResult | null>(null)
  const [testElapsedMs, setTestElapsedMs] = useState<number | null>(null)
  const [copied, setCopied] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    setLoadErr(null)
    try {
      const res = await getVoiceTranscriptionConfig()
      setData(res)
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // 下载进度轮询
  const dlPhase = data?.local.download?.phase ?? 'idle'
  useEffect(() => {
    if (dlPhase !== 'downloading') return
    const timer = setInterval(async () => {
      try {
        const res = await getVoiceTranscriptionConfig()
        setData(res)
      } catch {
        /* 轮询单次失败静默忽略 */
      }
    }, 1500)
    return () => clearInterval(timer)
  }, [dlPhase])

  const apply = useCallback(async (patch: VoiceTranscriptionConfigUpdate) => {
    setSaving(true)
    setSaveErr(null)
    try {
      const res = await updateVoiceTranscriptionConfig(patch)
      setData(res)
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }, [])

  const handleDownloadModel = useCallback(async () => {
    setDlReqErr(null)
    try {
      const res = await startTranscriptionModelDownload()
      setData(res)
    } catch (e) {
      setDlReqErr(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const handleRunTest = useCallback(async () => {
    if (!testFile) return
    setTesting(true)
    setTestErr(null)
    setTestResult(null)
    setTestElapsedMs(null)
    const t0 = performance.now()
    try {
      const override: Partial<VoiceTranscriptionConfigUpdate> = {}
      if (baseUrlDraft !== null) override.base_url = baseUrlDraft.trim()
      if (apiKeyDraft !== null) override.api_key = apiKeyDraft.trim()
      if (modelDraft !== null) override.model = modelDraft.trim()
      if (promptDraft !== null) override.prompt = promptDraft.trim()
      if (localDirDraft !== null) override.local_model_dir = localDirDraft.trim()

      const res = await transcribeAudioFile(testFile, {
        includeTimestamps: testTimestamps,
        test: true,
        override: Object.keys(override).length > 0 ? override : undefined,
      })
      setTestResult(res)
      setTestElapsedMs(Math.round(performance.now() - t0))
    } catch (e) {
      setTestErr(e instanceof Error ? e.message : String(e))
    } finally {
      setTesting(false)
    }
  }, [testFile, testTimestamps, baseUrlDraft, apiKeyDraft, modelDraft, promptDraft, localDirDraft])

  const handleCopyResult = useCallback(async () => {
    if (!testResult?.text) return
    try {
      await navigator.clipboard.writeText(testResult.text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* ignore */
    }
  }, [testResult])

  const selectedProvider = useMemo(() => {
    if (!data) return undefined
    return data.eligible_providers.find((p) => p.id === data.config.provider_id)
  }, [data])

  const modelSuggestions = useMemo(() => {
    if (!data) return []
    const pid = data.config.provider_id
    const rec = (pid && data.recommended_models[pid]) || data.recommended_models._default || []
    const fromProvider = (selectedProvider?.models ?? []).map((m) => m.id)
    const merged = Array.from(new Set([...rec, ...fromProvider]))
    return merged.map((id) => ({ value: id, label: id }))
  }, [data, selectedProvider])

  if (loading) {
    return (
      <div style={{ padding: 24, display: 'flex', alignItems: 'center', gap: 8 }}>
        <Loader2 size={16} className="spin" /> 加载语音转写配置…
      </div>
    )
  }

  if (loadErr || !data) {
    return (
      <div style={{ padding: 24, color: 'var(--accent-red)' }}>
        <AlertCircle size={16} /> 加载失败：{loadErr ?? '未知错误'}
        <button type="button" className="toolbar-action-btn" onClick={refresh} style={{ marginLeft: 12 }}>
          重试
        </button>
      </div>
    )
  }

  const { config, local, capabilities, env_fallback } = data

  return (
    <div className="voice-settings">
      <header className="voice-settings-hero">
        <div className="voice-settings-hero-icon">
          <FileAudio size={20} />
        </div>
        <div className="voice-settings-hero-text">
          <h2>语音转写</h2>
          <p>
            将录音或音视频文件转为文字，供对话附件通过 <code>read_file</code> / <code>transcribe_audio</code> 工具读取。
            支持<strong>本地 SenseVoice 全离线</strong>（与流式语音共享模型）与{' '}
            <strong>OpenAI 兼容接口</strong>（OpenAI / Groq / 硅基流动 / 本地 Whisper 服务）。
          </p>
        </div>
      </header>

      {/* ── 总开关 ───────────────────────────────────────────────── */}
      <section className="voice-settings-section">
        <h3>总开关</h3>
        <div className="voice-radio-group">
          <RadioCard
            checked={config.enabled}
            onChange={() => void apply({ enabled: true })}
            title="启用语音转写"
            desc="允许柳如烟通过 read_file / transcribe_audio 工具转写本地录音或上传的音视频附件"
          />
          <RadioCard
            checked={!config.enabled}
            onChange={() => void apply({ enabled: false })}
            title="停用"
            desc="停用后工具调用将提示用户先在设置中开启（下方测试区仍可用）"
          />
        </div>
      </section>

      {/* ── 转写引擎选择 ─────────────────────────────────────────── */}
      <section className="voice-settings-section">
        <h3>转写引擎</h3>
        <div className="voice-radio-group">
          <RadioCard
            checked={config.provider_type === 'local_sensevoice'}
            onChange={() => void apply({ provider_type: 'local_sensevoice' })}
            title="本地 · SenseVoice（默认 · 免 Key 全离线）"
            desc="复用本地 SenseVoice ONNX + Silero VAD 切句，零成本不出本机；Windows 原生支持 .wav/.mp3/.m4a/.aac/.wma/.mp4，安装 ffmpeg 可解锁全格式"
          />
          <RadioCard
            checked={config.provider_type === 'openai_compat'}
            onChange={() => void apply({ provider_type: 'openai_compat' })}
            title="云端 / 网关 · OpenAI 兼容（/v1/audio/transcriptions）"
            desc="对接 OpenAI（whisper-1 / gpt-4o-transcribe）、Groq（免费极速 whisper-large-v3-turbo）、硅基流动或本地 Whisper 服务"
          />
        </div>
      </section>

      {/* ── 引擎专属配置 ─────────────────────────────────────────── */}
      {config.provider_type === 'local_sensevoice' ? (
        <section className="voice-settings-section">
          <h3>本地 SenseVoice 配置</h3>

          <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
            {local.ready ? (
              <span style={{ color: 'var(--accent-green, #22c55e)', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <CheckCircle2 size={14} /> 本地模型已就绪（{local.dir}）
                {local.vad_ready ? ' · VAD 切句可用' : ' · 未检测到 silero_vad.onnx，将按固定时长分段'}
              </span>
            ) : (
              <div>
                <span style={{ color: '#f59e0b', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                  <AlertCircle size={14} /> 本地模型未就绪（约 230MB，下载后与流式语音共用，永久离线）
                </span>
                {local.download?.phase === 'downloading' ? (
                  <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 6 }}>
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                      <Loader2 size={14} className="spin" />
                      正在下载 {local.download.label}（{local.download.files_done}/{local.download.files_total}）
                      {local.download.percent >= 0 ? ` · ${local.download.percent}%` : ''}
                    </span>
                    {local.download.percent >= 0 ? (
                      <div
                        style={{
                          width: 320,
                          maxWidth: '100%',
                          height: 6,
                          borderRadius: 3,
                          background: 'var(--bg-tertiary)',
                          overflow: 'hidden',
                        }}
                      >
                        <div
                          style={{
                            width: `${local.download.percent}%`,
                            height: '100%',
                            background: 'var(--primary-color, #3b82f6)',
                            transition: 'width 0.3s',
                          }}
                        />
                      </div>
                    ) : null}
                  </div>
                ) : (
                  <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                    <button
                      type="button"
                      className="toolbar-action-btn"
                      onClick={() => void handleDownloadModel()}
                    >
                      <Download size={14} /> 下载模型（约 230MB）
                    </button>
                    {local.download?.phase === 'error' ? (
                      <span style={{ color: 'var(--accent-red)', fontSize: 12.5 }}>
                        下载失败：{local.download.error}
                      </span>
                    ) : null}
                    {dlReqErr ? (
                      <span style={{ color: 'var(--accent-red)', fontSize: 12.5 }}>{dlReqErr}</span>
                    ) : null}
                  </div>
                )}
              </div>
            )}
          </div>

          <div style={{ fontSize: 12.5, marginTop: 4 }}>
            {capabilities.ffmpeg ? (
              <span style={{ color: 'var(--accent-green, #22c55e)', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <CheckCircle2 size={13} /> 已检测到 ffmpeg，支持 .wav / .mp3 / .m4a / .ogg / .flac / .mp4 全格式本地转写
              </span>
            ) : capabilities.local_winrt ? (
              <span className="voice-settings-hint" style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <CheckCircle2 size={13} style={{ color: 'var(--accent-green, #22c55e)', flexShrink: 0 }} />
                未检测到 ffmpeg：已启用 Windows 原生解码，免配置直接支持 .wav / .mp3 / .m4a / .aac / .wma / .mp4；如需 .ogg / .opus / .webm 可安装 ffmpeg。
              </span>
            ) : (
              <span className="voice-settings-hint" style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <AlertCircle size={13} style={{ color: '#f59e0b', flexShrink: 0 }} />
                未检测到 ffmpeg：当前原生支持 .wav；若需本地直转 .mp3 / .m4a，请安装 ffmpeg（或切至 OpenAI 兼容引擎）。
              </span>
            )}
          </div>

          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', marginTop: 6, fontSize: 13 }}>
            <input
              type="checkbox"
              checked={config.local_use_vad}
              onChange={(e) => void apply({ local_use_vad: e.target.checked })}
            />
            <span>启用 Silero VAD 自动按停顿切句（长录音推荐开启，可生成分段时间戳）</span>
          </label>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 12, marginTop: 4 }}>
            <Field label="单段最大时长（秒）">
              <input
                className="voice-input"
                type="number"
                min={5}
                max={60}
                value={config.local_max_segment_sec}
                onChange={(e) => {
                  const v = Number(e.target.value)
                  if (Number.isFinite(v) && v >= 5 && v <= 60) {
                    void apply({ local_max_segment_sec: v })
                  }
                }}
              />
            </Field>
            <Field label="推理线程数">
              <input
                className="voice-input"
                type="number"
                min={1}
                max={16}
                value={config.local_num_threads}
                onChange={(e) => {
                  const v = parseInt(e.target.value, 10)
                  if (Number.isFinite(v) && v >= 1 && v <= 16) {
                    void apply({ local_num_threads: v })
                  }
                }}
              />
            </Field>
          </div>

          <Field label="自定义模型目录（留空自动复用默认共享目录）">
            <input
              className="voice-input"
              type="text"
              value={localDirDraft ?? config.local_model_dir}
              placeholder="留空自动探测 <data_dir>/models/voice_stream"
              onChange={(e) => setLocalDirDraft(e.target.value)}
              onBlur={() => {
                if (localDirDraft !== null) {
                  const v = localDirDraft.trim()
                  setLocalDirDraft(null)
                  if (v !== config.local_model_dir) void apply({ local_model_dir: v })
                }
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
              }}
            />
          </Field>
        </section>
      ) : (
        <section className="voice-settings-section">
          <h3>OpenAI 兼容接口配置</h3>

          <Field label="服务商来源">
            <select
              className="voice-select"
              value={config.provider_id}
              onChange={(e) => {
                const pid = e.target.value
                const rec = (pid && data.recommended_models[pid]?.[0]) || ''
                const patch: VoiceTranscriptionConfigUpdate = { provider_id: pid }
                if (rec && (!config.model || config.model === 'whisper-1' || pid === 'groq' || pid === 'siliconflow')) {
                  patch.model = rec
                }
                void apply(patch)
              }}
            >
              <option value="">— 自定义端点（手动填写 Base URL / Key 或使用环境变量）—</option>
              {data.eligible_providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                  {p.is_aggregator ? ' · 聚合站' : ''}
                  {p.has_api_key ? '' : ' · 未配 Key'}
                </option>
              ))}
            </select>
            {selectedProvider ? (
              selectedProvider.has_api_key ? (
                <p className="voice-settings-hint">
                  ✓ 复用「模型服务」凭证（base_url: <code>{selectedProvider.base_url || '未填'}</code>）
                </p>
              ) : (
                <p className="voice-settings-hint" style={{ color: 'var(--accent-red)' }}>
                  ⚠ 「{selectedProvider.name}」尚未配置 API Key，请前往「设置 → 模型服务」填写，或切为自定义端点。
                </p>
              )
            ) : null}
          </Field>

          {!config.provider_id ? (
            <>
              <Field label="Base URL">
                <input
                  className="voice-input"
                  type="text"
                  value={baseUrlDraft ?? config.base_url}
                  placeholder={env_fallback.has_openai_api_key ? env_fallback.base_url : 'https://api.openai.com/v1 或 http://127.0.0.1:8000/v1'}
                  onChange={(e) => setBaseUrlDraft(e.target.value)}
                  onBlur={() => {
                    if (baseUrlDraft !== null) {
                      const v = baseUrlDraft.trim()
                      setBaseUrlDraft(null)
                      if (v !== config.base_url) void apply({ base_url: v })
                    }
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                  }}
                />
              </Field>

              <Field label="API Key">
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <input
                    className="voice-input"
                    type={showApiKey ? 'text' : 'password'}
                    value={
                      apiKeyDraft !== null
                        ? apiKeyDraft
                        : config.has_api_key
                        ? '••••••已配置'
                        : ''
                    }
                    placeholder={
                      env_fallback.has_openai_api_key
                        ? '已检测到环境变量 OPENAI_API_KEY（填写可覆盖）'
                        : 'sk-...（本地 Whisper 服务可留空）'
                    }
                    onFocus={() => {
                      if (config.has_api_key && apiKeyDraft === null) {
                        setApiKeyDraft('')
                      }
                    }}
                    onChange={(e) => setApiKeyDraft(e.target.value)}
                    onBlur={() => {
                      if (apiKeyDraft !== null) {
                        const v = apiKeyDraft.trim()
                        setApiKeyDraft(null)
                        void apply({ api_key: v })
                      }
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                    }}
                    style={{ flex: 1 }}
                  />
                  <button
                    type="button"
                    className="toolbar-action-btn"
                    onClick={() => setShowApiKey((prev) => !prev)}
                    title={showApiKey ? '隐藏' : '显示'}
                    style={{ flexShrink: 0 }}
                  >
                    {showApiKey ? <Eye size={14} /> : <EyeOff size={14} />}
                  </button>
                </div>
                {config.has_api_key ? (
                  <p className="voice-settings-hint">✓ 已保存自定义 API Key（聚焦清空后失焦可清除）</p>
                ) : env_fallback.has_openai_api_key ? (
                  <p className="voice-settings-hint">✓ 将自动回退使用环境变量 OPENAI_API_KEY</p>
                ) : null}
              </Field>
            </>
          ) : null}

          <Field label="转写模型（Model）">
            <Combobox
              value={modelDraft ?? config.model}
              options={modelSuggestions}
              placeholder="whisper-1 / gpt-4o-transcribe / whisper-large-v3-turbo"
              onChange={(v) => setModelDraft(v)}
              onCommit={(v) => {
                const trimmed = v.trim()
                setModelDraft(null)
                if (trimmed && trimmed !== config.model) {
                  void apply({ model: trimmed })
                }
              }}
              ariaLabel="转写模型名称"
            />
          </Field>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 12 }}>
            <Field label="响应格式">
              <select
                className="voice-select"
                value={config.response_format}
                onChange={(e) =>
                  void apply({ response_format: e.target.value as VoiceTranscriptionResponseFormat })
                }
              >
                <option value="json">json（默认，全模型兼容）</option>
                <option value="verbose_json">verbose_json（含分段时间戳）</option>
                <option value="text">text（纯文本）</option>
                <option value="srt">srt（字幕格式）</option>
                <option value="vtt">vtt（WebVTT 字幕）</option>
              </select>
            </Field>

            <Field label="请求超时（秒）">
              <input
                className="voice-input"
                type="number"
                min={10}
                max={600}
                value={config.timeout_sec}
                onChange={(e) => {
                  const v = parseInt(e.target.value, 10)
                  if (Number.isFinite(v) && v >= 10 && v <= 600) {
                    void apply({ timeout_sec: v })
                  }
                }}
              />
            </Field>
          </div>
        </section>
      )}

      {/* ── 通用偏好 ─────────────────────────────────────────────── */}
      <section className="voice-settings-section">
        <h3>转写偏好</h3>
        <Field label="默认语种">
          <select
            className="voice-select"
            value={config.language}
            onChange={(e) => void apply({ language: e.target.value })}
          >
            {LANGUAGE_CHOICES.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
        </Field>

        <Field label="默认词汇提示（Prompt，可选，专有名词 / 人名纠偏）">
          <input
            className="voice-input"
            type="text"
            value={promptDraft ?? config.prompt}
            placeholder="例如：柳如烟、FastAPI、Whisper（仅 OpenAI 兼容接口生效）"
            onChange={(e) => setPromptDraft(e.target.value)}
            onBlur={() => {
              if (promptDraft !== null) {
                const v = promptDraft.trim()
                setPromptDraft(null)
                if (v !== config.prompt) void apply({ prompt: v })
              }
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
            }}
          />
        </Field>

        <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', marginTop: 6, fontSize: 13 }}>
          <input
            type="checkbox"
            checked={config.auto_polish}
            onChange={(e) => void apply({ auto_polish: e.target.checked })}
          />
          <span>自动清理句首独立语气词（如「呃」「嗯」；默认关闭以保留忠实原文）</span>
        </label>
      </section>

      {/* ── 在线测试 ─────────────────────────────────────────────── */}
      <section className="voice-settings-section">
        <h3>测试转写</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <input
            ref={fileInputRef}
            type="file"
            accept="audio/*,video/*,.wav,.mp3,.m4a,.ogg,.flac,.webm,.mp4"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files?.[0] ?? null
              setTestFile(f)
              setTestErr(null)
              setTestResult(null)
            }}
          />
          <button
            type="button"
            className="toolbar-action-btn"
            onClick={() => fileInputRef.current?.click()}
          >
            <Upload size={14} />
            <span>{testFile ? testFile.name : '选择音视频文件'}</span>
          </button>

          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12.5, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={testTimestamps}
              onChange={(e) => setTestTimestamps(e.target.checked)}
            />
            <span>输出时间戳</span>
          </label>

          <button
            type="button"
            className="toolbar-action-btn"
            disabled={!testFile || testing}
            onClick={() => void handleRunTest()}
          >
            {testing ? <Loader2 size={14} className="spin" /> : <PlayCircle size={14} />}
            <span>{testing ? '转写中…' : '开始转写'}</span>
          </button>
        </div>

        {testErr ? (
          <div className="voice-settings-warn" style={{ marginTop: 6 }}>
            <AlertCircle size={14} style={{ flexShrink: 0 }} />
            <span style={{ whiteSpace: 'pre-wrap' }}>{testErr}</span>
          </div>
        ) : null}

        {testResult ? (
          <div
            style={{
              marginTop: 8,
              padding: 12,
              borderRadius: 8,
              border: '1px solid var(--border-color)',
              background: 'var(--bg-secondary)',
              display: 'flex',
              flexDirection: 'column',
              gap: 8,
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <span className="voice-settings-hint" style={{ margin: 0 }}>
                引擎：<code>{testResult.backend}</code> · 模型：<code>{testResult.model}</code>
                {testResult.duration_sec !== undefined ? ` · 时长 ${testResult.duration_sec}s` : ''}
                {testElapsedMs !== null ? ` · 耗时 ${(testElapsedMs / 1000).toFixed(2)}s` : ''}
              </span>
              <button type="button" className="toolbar-action-btn" onClick={() => void handleCopyResult()}>
                {copied ? <CheckCircle2 size={13} /> : <Copy size={13} />}
                <span>{copied ? '已复制' : '复制正文'}</span>
              </button>
            </div>
            <div style={{ fontSize: 13.5, lineHeight: 1.65, whiteSpace: 'pre-wrap', color: 'var(--text-color)' }}>
              {testResult.text || '（未识别出有效语音内容）'}
            </div>
            {testResult.segments && testResult.segments.length > 0 ? (
              <div
                style={{
                  borderTop: '1px solid var(--border-color)',
                  paddingTop: 8,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 4,
                  maxHeight: 200,
                  overflowY: 'auto',
                  fontSize: 12,
                  color: 'var(--text-secondary)',
                }}
              >
                {testResult.segments.map((seg, idx) => (
                  <div key={idx}>
                    <code>
                      [{formatTime(seg.start)} - {formatTime(seg.end)}]
                    </code>{' '}
                    {seg.speaker ? <strong>{seg.speaker}: </strong> : null}
                    {seg.text}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
      </section>

      {/* ── Footer ───────────────────────────────────────────────── */}
      <footer className="voice-settings-footer">
        {saving ? (
          <span className="voice-settings-status">
            <Loader2 size={13} className="spin" /> 保存中…
          </span>
        ) : saveErr ? (
          <span className="voice-settings-status err">
            <AlertCircle size={13} /> 保存失败：{saveErr}
          </span>
        ) : !data.available ? (
          <span className="voice-settings-status" style={{ color: '#f59e0b' }}>
            <AlertCircle size={13} /> {data.reason}
          </span>
        ) : (
          <span className="voice-settings-status ok">
            <CheckCircle2 size={13} /> 已就绪并同步（修改即时生效）
          </span>
        )}
      </footer>
    </div>
  )
}

/* ── 内部自包含子组件（删模块零残留） ─────────────────────────── */

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 4 }}>
      <span style={{ fontSize: 12.5, fontWeight: 500, color: 'var(--text-secondary)' }}>{label}</span>
      {children}
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

function Combobox({
  value,
  options,
  placeholder,
  onChange,
  onCommit,
  ariaLabel,
}: {
  value: string
  options: { value: string; label: string }[]
  placeholder?: string
  onChange: (v: string) => void
  onCommit: (v: string) => void
  ariaLabel?: string
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const hasOptions = options.length > 0

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

  const filtered = useMemo(() => {
    const q = value.trim().toLowerCase()
    if (!q) return options
    if (options.some((o) => o.value.toLowerCase() === q)) return options
    return options.filter(
      (o) => o.value.toLowerCase().includes(q) || o.label.toLowerCase().includes(q),
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
        onBlur={() => {
          onCommit(value)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            setOpen(false)
          } else if (e.key === 'Enter') {
            setOpen(false)
            ;(e.target as HTMLInputElement).blur()
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
            e.preventDefault()
            setOpen((v) => !v)
            inputRef.current?.focus()
          }}
          tabIndex={-1}
          aria-label="展开推荐模型列表"
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
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                onChange(o.value)
                onCommit(o.value)
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
