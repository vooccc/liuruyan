/**
 * CharacterWorkshop 「🔊 语音」tab：编辑角色级 TTS override。
 *
 * 数据来源：GET /tts/agents/{id}/override（角色级）+ GET /tts/config（全局，作 placeholder）
 * 数据写回：PUT /tts/agents/{id}/override（每字段失焦/切换时立即保存）
 * 试听：POST /tts/test 带 agent_id
 *
 * 设计要点：
 * - 字段留空 = 「继承全局」（placeholder 显示当前全局值）
 * - 协议切换：继承全局 / 强制 OpenAI / 强制 GPT-SoVITS（通过 provider_type 是否在 override 里）
 * - 试听用角色音色（带 agent_id），不污染全局配置
 *
 * 设计文档：docs/architecture_spec.md §M5
 */
import { AlertCircle, Loader2, PlayCircle, Trash2, Upload } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

import {
  blobToAudioURL,
  deleteAgentReference,
  deleteAgentTTSOverride,
  getAgentReferenceUrl,
  getAgentTTSOverride,
  getTTSConfig,
  listAgentReferences,
  testTTS,
  TTSAgentOverride,
  TTSAgentReference,
  TTSConfigResponse,
  updateAgentTTSOverride,
  uploadAgentReference,
} from '../api/tts'

interface VoiceTabProps {
  agentId: string
  agentName?: string
}

/* ── 主组件 ──────────────────────────────────────────────────── */

export default function CharacterWorkshopVoiceTab({ agentId, agentName }: VoiceTabProps) {
  // 本地草稿：用户在表单里改的字段。空字符串/undefined 都视为"未设"。
  const [draft, setDraft] = useState<TTSAgentOverride>({})
  // 全局配置（作为 placeholder 显示，让用户知道"留空 = 继承这个值"）
  const [globalCfg, setGlobalCfg] = useState<TTSConfigResponse | null>(null)
  // 角色目录里已有的参考音频列表
  const [refFiles, setRefFiles] = useState<TTSAgentReference[]>([])
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  // 试听
  const [tryText, setTryText] = useState('你好，这是我的声音')
  const [testing, setTesting] = useState(false)
  const [testErr, setTestErr] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const lastUrlRef = useRef<string | null>(null)

  // 刷新角色参考音频列表（上传/删除后调一次）
  const refreshRefFiles = useCallback(async () => {
    try {
      const res = await listAgentReferences(agentId)
      setRefFiles(res.files)
    } catch (e) {
      console.error('[VoiceTab] list references failed:', e)
    }
  }, [agentId])

  // 加载：角色 override + 全局配置 + 参考音频列表（并行）
  useEffect(() => {
    let alive = true
    setLoading(true)
    setLoadErr(null)
    Promise.all([
      getAgentTTSOverride(agentId),
      getTTSConfig(),
      listAgentReferences(agentId),
    ])
      .then(([overrideRes, cfgRes, refsRes]) => {
        if (!alive) return
        setDraft(overrideRes.override || {})
        setGlobalCfg(cfgRes)
        setRefFiles(refsRes.files)
      })
      .catch((e) => {
        if (!alive) return
        setLoadErr(String(e?.message || e))
      })
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [agentId])

  // 字段更新：本地立即变 + PUT 写后端（防抖留待用户失焦时——这版直接每次 change 触发）
  // 简单起见每个字段 change 立即 PUT；字段少（最多 6 个），频率不高
  const updateField = useCallback(
    async <K extends keyof TTSAgentOverride>(key: K, value: TTSAgentOverride[K] | '') => {
      // 本地：空字符串语义 = 移除该字段（draft 里留空）
      const nextDraft = { ...draft }
      if (value === '' || value === undefined) {
        delete nextDraft[key]
      } else {
        ;(nextDraft as Record<string, unknown>)[key as string] = value
      }
      setDraft(nextDraft)
      // 远端：传 ""（空字符串）告诉后端清字段；非空就覆盖
      setSaving(true)
      try {
        await updateAgentTTSOverride(agentId, { [key]: value === '' ? '' : value } as TTSAgentOverride)
      } catch (e) {
        console.error('[VoiceTab] update field failed:', e)
      } finally {
        setSaving(false)
      }
    },
    [agentId, draft],
  )

  const handleClearAll = useCallback(async () => {
    setSaving(true)
    try {
      await deleteAgentTTSOverride(agentId)
      setDraft({})
    } catch (e) {
      console.error('[VoiceTab] clear failed:', e)
    } finally {
      setSaving(false)
    }
  }, [agentId])

  // 上传参考音频：上传完自动把 override.gptsovits_refer_wav_path 设为 ./tts/<filename>
  const handleUploadFile = useCallback(
    async (file: File) => {
      setSaving(true)
      setTestErr(null)
      try {
        const res = await uploadAgentReference(agentId, file)
        // 自动选中刚上传的文件
        await updateField('gptsovits_refer_wav_path', res.rel_path)
        // 刷新列表
        await refreshRefFiles()
      } catch (e: any) {
        setTestErr(`上传失败：${e?.detail || e?.message || e}`)
      } finally {
        setSaving(false)
      }
    },
    [agentId, refreshRefFiles, updateField],
  )

  // 选中已有音频
  const handlePickReference = useCallback(
    (filename: string) => {
      void updateField('gptsovits_refer_wav_path', `./tts/${filename}`)
    },
    [updateField],
  )

  // 删除参考音频；若 draft 当前选中此文件，同时清空 override 中的 refer_wav_path
  const handleDeleteReference = useCallback(
    async (filename: string) => {
      setSaving(true)
      try {
        await deleteAgentReference(agentId, filename)
        // 若 draft 引用了被删的文件，连带清掉 refer_wav_path
        if (draft.gptsovits_refer_wav_path === `./tts/${filename}`) {
          await updateField('gptsovits_refer_wav_path', '')
        }
        await refreshRefFiles()
      } catch (e: any) {
        setTestErr(`删除失败：${e?.detail || e?.message || e}`)
      } finally {
        setSaving(false)
      }
    },
    [agentId, draft.gptsovits_refer_wav_path, refreshRefFiles, updateField],
  )

  const handleTest = useCallback(async () => {
    if (!tryText.trim()) return
    setTesting(true)
    setTestErr(null)
    try {
      const blob = await testTTS(tryText, { agentId })
      // 释放上一次 URL，避免内存泄漏
      if (lastUrlRef.current) URL.revokeObjectURL(lastUrlRef.current)
      const url = blobToAudioURL(blob)
      lastUrlRef.current = url
      if (audioRef.current) {
        audioRef.current.src = url
        audioRef.current.play().catch(() => {})
      }
    } catch (e: any) {
      setTestErr(String(e?.detail || e?.message || e))
    } finally {
      setTesting(false)
    }
  }, [agentId, tryText])

  // 释放 audio URL
  useEffect(() => {
    return () => {
      if (lastUrlRef.current) URL.revokeObjectURL(lastUrlRef.current)
    }
  }, [])

  if (loading) {
    return <div className="workshop-loading">加载语音配置中…</div>
  }
  if (loadErr) {
    return (
      <div className="workshop-error-banner" style={{ marginBottom: 8 }}>
        ⚠️ 加载失败：{loadErr}
      </div>
    )
  }

  // 当前生效协议：override.provider_type 优先，否则 fallback 全局
  const effectiveProviderType =
    draft.provider_type || globalCfg?.config.provider_type || 'openai_compat'
  const isGptSovits = effectiveProviderType === 'gptsovits_compat'

  return (
    <div className="voice-settings" style={{ padding: 0 }}>
      <header className="voice-settings-hero">
        <div className="voice-settings-hero-text">
          <h2>{agentName || '该角色'}的语音</h2>
          <p>
            为这个角色单独绑一个音色。<strong>所有字段留空 = 跟随全局默认</strong>
            （在「设置 → 辅助功能 → 语音输出」配置）。
            修改会自动保存。
            {saving && (
              <span style={{ marginLeft: 8, color: 'var(--text-muted)' }}>
                <Loader2 size={12} className="spin" /> 保存中…
              </span>
            )}
          </p>
        </div>
      </header>

      {/* ── 协议选择 ───────────────────────────────────────────── */}
      <section className="voice-settings-section">
        <h3>后端协议（可选）</h3>
        <div className="voice-radio-group">
          <ProtocolCard
            checked={!draft.provider_type}
            onChange={() => updateField('provider_type', '')}
            title="跟随全局"
            desc={`当前全局：${labelOfProvider(globalCfg?.config.provider_type)}`}
          />
          <ProtocolCard
            checked={draft.provider_type === 'openai_compat'}
            onChange={() => updateField('provider_type', 'openai_compat')}
            title="OpenAI 兼容"
            desc="该角色用 SiliconFlow / OpenAI / Groq 等云端 TTS"
          />
          <ProtocolCard
            checked={draft.provider_type === 'gptsovits_compat'}
            onChange={() => updateField('provider_type', 'gptsovits_compat')}
            title="GPT-SoVITS / Qwen3TTS"
            desc="该角色用本地 GPT-SoVITS（zero-shot 参考音频）"
          />
          <ProtocolCard
            checked={draft.provider_type === 'baidu_compat'}
            onChange={() => updateField('provider_type', 'baidu_compat')}
            title="百度云"
            desc="该角色强制使用全局百度云凭证与音色"
          />
          <ProtocolCard
            checked={draft.provider_type === 'aliyun_compat'}
            onChange={() => updateField('provider_type', 'aliyun_compat')}
            title="阿里云"
            desc="该角色强制使用全局阿里云凭证与音色"
          />
        </div>
      </section>

      {/* ── 字段：根据当前生效协议展示 ────────────────────────── */}
      {!isGptSovits ? (
        <OpenAIOverrideFields
          draft={draft}
          globalCfg={globalCfg}
          onChange={updateField}
        />
      ) : (
        <GPTSoVITSOverrideFields
          agentId={agentId}
          draft={draft}
          globalCfg={globalCfg}
          refFiles={refFiles}
          onChange={updateField}
          onUpload={handleUploadFile}
          onPick={handlePickReference}
          onDelete={handleDeleteReference}
        />
      )}

      {/* ── 试听 ────────────────────────────────────────────── */}
      <section className="voice-settings-section">
        <h3>试听</h3>
        <p className="voice-settings-hint">
          会用<strong>该角色的音色</strong>合成（角色 override 优先于全局）。改完字段直接点试听就能听效果。
        </p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <textarea
            value={tryText}
            onChange={(e) => setTryText(e.target.value)}
            maxLength={500}
            rows={2}
            style={{ flex: 1, fontSize: 13, padding: 8 }}
            placeholder="输入试听文本（≤500 字符）"
          />
          <button
            type="button"
            className="toolbar-action-btn"
            onClick={handleTest}
            disabled={testing || !tryText.trim()}
            style={{ minWidth: 90 }}
          >
            {testing ? (
              <>
                <Loader2 size={14} className="spin" /> 合成中…
              </>
            ) : (
              <>
                <PlayCircle size={14} /> 试听
              </>
            )}
          </button>
        </div>
        <audio ref={audioRef} controls style={{ marginTop: 12, width: '100%' }} />
        {testErr && (
          <div
            style={{
              color: 'var(--accent-red)',
              marginTop: 8,
              fontSize: 13,
              userSelect: 'text',
              WebkitUserSelect: 'text',
              cursor: 'text',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
            }}
          >
            <AlertCircle size={13} /> {testErr}
          </div>
        )}
      </section>

      {/* ── 清除按钮 ────────────────────────────────────────── */}
      {Object.keys(draft).length > 0 && (
        <section className="voice-settings-section">
          <button
            type="button"
            className="toolbar-action-btn"
            onClick={handleClearAll}
            style={{ color: 'var(--accent-red)' }}
          >
            <Trash2 size={13} /> 清除该角色的所有语音配置（回退全局）
          </button>
        </section>
      )}
    </div>
  )
}

/* ── 子组件 ─────────────────────────────────────────────────── */

function ProtocolCard({
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
      <span className="voice-radio-indicator">
        {checked ? <span className="dot" /> : null}
      </span>
      <span>
        <span className="voice-radio-title">{title}</span>
        <span className="voice-radio-desc">{desc}</span>
      </span>
    </button>
  )
}

function OpenAIOverrideFields({
  draft,
  globalCfg,
  onChange,
}: {
  draft: TTSAgentOverride
  globalCfg: TTSConfigResponse | null
  onChange: <K extends keyof TTSAgentOverride>(key: K, value: TTSAgentOverride[K] | '') => void
}) {
  const placeholderModel = globalCfg?.config.model || 'tts-1'
  const placeholderVoice = globalCfg?.config.voice || 'alloy'
  return (
    <section className="voice-settings-section">
      <h3>OpenAI 兼容字段</h3>

      <FieldRow label="Model">
        <input
          className="voice-input"
          type="text"
          value={draft.model || ''}
          placeholder={`继承全局：${placeholderModel}`}
          onChange={(e) => onChange('model', e.target.value || '')}
        />
      </FieldRow>

      <FieldRow label="Voice">
        <input
          className="voice-input"
          type="text"
          value={draft.voice || ''}
          placeholder={`继承全局：${placeholderVoice}`}
          onChange={(e) => onChange('voice', e.target.value || '')}
        />
      </FieldRow>

      <FieldRow label={`语速：${(draft.speed ?? globalCfg?.config.speed ?? 1.0).toFixed(2)}x`}>
        <input
          type="range"
          min={0.25}
          max={4.0}
          step={0.05}
          value={draft.speed ?? globalCfg?.config.speed ?? 1.0}
          onChange={(e) => onChange('speed', Number(e.target.value))}
          style={{ width: '100%' }}
        />
      </FieldRow>
    </section>
  )
}

function GPTSoVITSOverrideFields({
  agentId,
  draft,
  globalCfg,
  refFiles,
  onChange,
  onUpload,
  onPick,
  onDelete,
}: {
  agentId: string
  draft: TTSAgentOverride
  globalCfg: TTSConfigResponse | null
  refFiles: TTSAgentReference[]
  onChange: <K extends keyof TTSAgentOverride>(key: K, value: TTSAgentOverride[K] | '') => void
  onUpload: (file: File) => void | Promise<void>
  onPick: (filename: string) => void
  onDelete: (filename: string) => void | Promise<void>
}) {
  const phPrompt = globalCfg?.config.gptsovits_prompt_text || '（全局未配置）'
  const phLang = globalCfg?.config.gptsovits_text_lang || 'zh'
  const phUrl = globalCfg?.config.gptsovits_base_url || 'http://127.0.0.1:9880'
  const fileInputRef = useRef<HTMLInputElement>(null)
  // 当前 draft 选中的 rel_path（用于高亮列表项）
  const selectedRel = draft.gptsovits_refer_wav_path || ''
  // 是否选中"外部绝对路径"模式（即 refer_wav_path 不是 ./tts/ 开头，是绝对/外部路径）
  const usingExternal =
    selectedRel.length > 0 && !selectedRel.startsWith('./tts/') && !selectedRel.startsWith('.\\tts\\')

  return (
    <section className="voice-settings-section">
      <h3>GPT-SoVITS 字段</h3>
      <p className="voice-settings-hint">
        参考音频<strong>跟角色绑死</strong>——上传的文件会存到角色目录的 <code>tts/</code>{' '}
        子目录里。模板自带的参考音频会在新建角色时自动复制进来。
      </p>

      {/* 服务地址 */}
      <FieldRow label="服务地址">
        <input
          className="voice-input"
          type="text"
          value={draft.gptsovits_base_url || ''}
          placeholder={`继承全局：${phUrl}`}
          onChange={(e) => onChange('gptsovits_base_url', e.target.value || '')}
        />
      </FieldRow>

      {/* 角色音频文件管理 */}
      <FieldRow label="角色参考音频">
        {refFiles.length === 0 ? (
          <p className="voice-settings-hint" style={{ marginTop: 0 }}>
            该角色还没有参考音频文件。点下面「上传」按钮添加一个。
          </p>
        ) : (
          <div className="voice-ref-list">
            {refFiles.map((f) => {
              const checked = selectedRel === f.rel_path
              return (
                <div
                  key={f.filename}
                  className={`voice-ref-item ${checked ? 'checked' : ''}`}
                >
                  <button
                    type="button"
                    className="voice-ref-pick"
                    onClick={() => onPick(f.filename)}
                    title="使用此音频作为参考"
                  >
                    <span className="voice-radio-indicator">
                      {checked ? <span className="dot" /> : null}
                    </span>
                    <span className="voice-ref-name">{f.filename}</span>
                    <span className="voice-ref-size">{(f.size / 1024).toFixed(1)} KB</span>
                  </button>
                  <audio
                    controls
                    preload="none"
                    src={getAgentReferenceUrl(agentId, f.filename)}
                    style={{ height: 28, maxWidth: 220 }}
                  />
                  <button
                    type="button"
                    className="voice-ref-delete"
                    onClick={() => onDelete(f.filename)}
                    title="删除此音频"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              )
            })}
          </div>
        )}
        <div style={{ marginTop: 8 }}>
          <input
            ref={fileInputRef}
            type="file"
            accept="audio/*,.wav,.mp3,.ogg,.flac,.m4a"
            style={{ display: 'none' }}
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) {
                void onUpload(file)
                // 清掉 input 让同名文件能再次触发
                e.target.value = ''
              }
            }}
          />
          <button
            type="button"
            className="toolbar-action-btn"
            onClick={() => fileInputRef.current?.click()}
          >
            <Upload size={13} /> 上传新音频
          </button>
          <span className="voice-settings-hint" style={{ marginLeft: 8 }}>
            支持 mp3/wav/ogg/flac/m4a，≤20 MB。建议 5-10 秒清晰人声。
          </span>
        </div>
      </FieldRow>

      {/* 高级：填外部绝对路径（不放在角色目录里） */}
      <FieldRow label="或填外部绝对路径（高级）">
        <input
          className="voice-input"
          type="text"
          value={usingExternal ? selectedRel : ''}
          placeholder="如 D:\\audio\\external.mp3（一般不用，留空即可）"
          onChange={(e) => onChange('gptsovits_refer_wav_path', e.target.value || '')}
        />
        <p className="voice-settings-hint">
          {usingExternal
            ? '⚠️ 当前使用外部绝对路径——撤除角色时不会一起删，需要你自己管理。'
            : '正常情况下用上面的「上传」按钮即可，文件会跟角色绑死。'}
        </p>
      </FieldRow>

      {/* 文字稿 */}
      <FieldRow label="参考音频文字稿">
        <input
          className="voice-input"
          type="text"
          value={draft.gptsovits_prompt_text || ''}
          placeholder={`继承全局：${phPrompt}`}
          onChange={(e) => onChange('gptsovits_prompt_text', e.target.value || '')}
        />
        <p className="voice-settings-hint">
          中文可不填；日文/英文最好填上对应文字稿，提升克隆精度。
        </p>
      </FieldRow>

      {/* 文本语言 */}
      <FieldRow label="文本语言">
        <select
          className="voice-select"
          value={draft.gptsovits_text_lang || ''}
          onChange={(e) => onChange('gptsovits_text_lang', e.target.value || '')}
        >
          <option value="">继承全局：{phLang}</option>
          <option value="zh">zh · 中文</option>
          <option value="ja">ja · 日文</option>
          <option value="en">en · 英文</option>
        </select>
      </FieldRow>
    </section>
  )
}

function FieldRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="voice-settings-field">
      <label className="voice-settings-field-label">{label}</label>
      {children}
    </div>
  )
}

function labelOfProvider(t?: string) {
  if (t === 'aliyun_compat') return '阿里云'
  if (t === 'baidu_compat') return '百度云'
  if (t === 'tencent_compat') return '腾讯云'
  if (t === 'gptsovits_compat') return 'GPT-SoVITS / Qwen3TTS'
  if (t === 'openai_compat') return 'OpenAI 兼容'
  return '未配置'
}
