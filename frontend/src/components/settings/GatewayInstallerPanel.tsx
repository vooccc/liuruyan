/**
 * Gateway M5：一键 CLI 接入面板。
 *
 * 设计文档：_docs/GATEWAY_M5.md
 *
 * 用户故事：
 *   - 不想再在终端 export ANTHROPIC_BASE_URL=...，点一下"接入 Claude Code"就直接能用
 *   - 怕被改坏：每次操作前可"预览"看 diff、看会备份到哪
 *   - 怕装错：状态徽章明确显示当前指向（我们 / 其他 / 未装 / 外部改过）
 *   - 怕丢配置：每次 install 都备份；uninstall 还原最近备份
 */
import { useCallback, useEffect, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Copy,
  Eye,
  FolderOpen,
  Loader2,
  Play,
  Plug,
  PlugZap,
  RefreshCw,
  Trash2,
  X,
} from 'lucide-react'
import {
  type ClaudeDefaultModels,
  type ClaudeInstallOptions,
  type CodexInstallOptions,
  type GatewayInstallerEntry,
  type GatewayInstallerPreview,
  type GatewayInstallerState,
  type GatewayLauncherSnippet,
  type GatewayModelInfo,
  installGatewayInstaller,
  launchGatewayLauncherTerminal,
  listAdminModels,
  listGatewayInstallers,
  previewGatewayInstaller,
  uninstallGatewayInstaller,
} from '../../api/gateway'
import { type ProviderView, listProviders } from '../../api/providers'
import { useConfirmDialog } from '../ConfirmDialog'

interface Props {
  enabled: boolean
  /** GatewaySettings 拉好的 launcher snippet 列表；按 id 与 installer 1:1 配对（runnable 才显示启动子区）。 */
  launchers: GatewayLauncherSnippet[]
}

/** localStorage key：每个 launcher 上次选择的工作区（沿用 GatewaySettings.tsx 旧约定，迁移期间状态平滑）。 */
const CWD_STORAGE_PREFIX = 'lry.gw.cwd.'
/** localStorage key：用户上次选的 shell（PowerShell/POSIX）。 */
const SHELL_STORAGE_KEY = 'lry.gw.installer.shell.v1'

function _loadStoredCwd(id: string): string {
  try {
    return localStorage.getItem(CWD_STORAGE_PREFIX + id) ?? ''
  } catch {
    return ''
  }
}

function _saveStoredCwd(id: string, cwd: string): void {
  try {
    if (cwd) localStorage.setItem(CWD_STORAGE_PREFIX + id, cwd)
    else localStorage.removeItem(CWD_STORAGE_PREFIX + id)
  } catch {
    /* localStorage 满 / 隐私模式静默失败 */
  }
}

function _loadShellMode(): 'posix' | 'powershell' {
  try {
    const v = localStorage.getItem(SHELL_STORAGE_KEY)
    if (v === 'posix' || v === 'powershell') return v
  } catch {
    /* 静默 */
  }
  // 默认按平台猜：Win → powershell，其余 → posix
  if (typeof navigator !== 'undefined' && /win/i.test(navigator.platform || '')) return 'powershell'
  return 'posix'
}

/** Claude Code 三角色 → 当前选择的 provider:model（"" = 用 Claude 内置默认）。 */
type DefaultModelsState = Required<{
  [K in keyof ClaudeDefaultModels]: string
}>

const EMPTY_DEFAULT_MODELS: DefaultModelsState = { haiku: '', sonnet: '', opus: '' }

/** Claude Code 接入模式：gateway（本地转发）/ direct（直连 anthropic 协议服务商）。 */
interface ClaudeState {
  mode: 'gateway' | 'direct'
  /** direct 模式必填：柳如烟 provider id。 */
  directProvider: string
}
const EMPTY_CLAUDE: ClaudeState = { mode: 'gateway', directProvider: '' }

/** Codex 状态：defaultModel 必选，未选时一键接入按钮 disabled。
 * mode：gateway（经柳如烟本地转发）/ direct（直连『模型管理』里配置的服务商）。 */
interface CodexState {
  defaultModel: string
  reasoningEffort: 'minimal' | 'low' | 'medium' | 'high'
  mode: 'gateway' | 'direct'
  /** direct 模式必填：柳如烟 provider id。 */
  directProvider: string
}
const EMPTY_CODEX: CodexState = {
  defaultModel: '',
  reasoningEffort: 'high',
  mode: 'gateway',
  directProvider: '',
}

// localStorage keys —— 持久化用户在面板里的下拉选择
// 切设置 tab、关重开窗口都不丢；用户每次进面板看到上次填的，不用重选
const LS_KEY_CLAUDE = 'lry.gw.installer.claudeModels.v1'
const LS_KEY_CLAUDE_STATE = 'lry.gw.installer.claudeState.v1'
const LS_KEY_CODEX = 'lry.gw.installer.codexState.v1'

function _loadClaudeModels(): DefaultModelsState {
  try {
    const raw = localStorage.getItem(LS_KEY_CLAUDE)
    if (!raw) return EMPTY_DEFAULT_MODELS
    const parsed = JSON.parse(raw)
    return {
      haiku: typeof parsed?.haiku === 'string' ? parsed.haiku : '',
      sonnet: typeof parsed?.sonnet === 'string' ? parsed.sonnet : '',
      opus: typeof parsed?.opus === 'string' ? parsed.opus : '',
    }
  } catch {
    return EMPTY_DEFAULT_MODELS
  }
}

function _loadClaudeState(): ClaudeState {
  try {
    const raw = localStorage.getItem(LS_KEY_CLAUDE_STATE)
    if (!raw) return EMPTY_CLAUDE
    const parsed = JSON.parse(raw)
    return {
      mode: parsed?.mode === 'direct' ? 'direct' : 'gateway',
      directProvider: typeof parsed?.directProvider === 'string' ? parsed.directProvider : '',
    }
  } catch {
    return EMPTY_CLAUDE
  }
}

function _loadCodexState(): CodexState {
  try {
    const raw = localStorage.getItem(LS_KEY_CODEX)
    if (!raw) return EMPTY_CODEX
    const parsed = JSON.parse(raw)
    const eff = parsed?.reasoningEffort
    return {
      defaultModel: typeof parsed?.defaultModel === 'string' ? parsed.defaultModel : '',
      reasoningEffort:
        eff === 'minimal' || eff === 'low' || eff === 'medium' || eff === 'high'
          ? eff
          : 'high',
      mode: parsed?.mode === 'direct' ? 'direct' : 'gateway',
      directProvider: typeof parsed?.directProvider === 'string' ? parsed.directProvider : '',
    }
  } catch {
    return EMPTY_CODEX
  }
}

export default function GatewayInstallerPanel({ enabled, launchers }: Props) {
  // 默认展开（用户反馈：与其他 tab 折叠风格一致，常用功能直接可见）
  const [open, setOpen] = useState(true)
  const [items, setItems] = useState<GatewayInstallerEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<{ id: string; data: GatewayInstallerPreview } | null>(null)
  // Claude Code 三角色 → 选中的柳如烟 model（ccswitch 风格）。lazy init 从 localStorage 恢复
  const [claudeModels, setClaudeModels] = useState<DefaultModelsState>(_loadClaudeModels)
  // Claude Code 接入模式（网关 / 直连）
  const [claudeState, setClaudeState] = useState<ClaudeState>(_loadClaudeState)
  // Codex 参数（default_model 必填 + reasoning_effort）。lazy init 从 localStorage 恢复
  const [codexState, setCodexState] = useState<CodexState>(_loadCodexState)

  // 任一选择变化都立刻持久化（不等关面板，避免崩溃丢失）
  useEffect(() => {
    try {
      localStorage.setItem(LS_KEY_CLAUDE, JSON.stringify(claudeModels))
    } catch {
      // localStorage 满 / 隐身模式 / 等异常时静默失败，不影响功能
    }
  }, [claudeModels])
  useEffect(() => {
    try {
      localStorage.setItem(LS_KEY_CLAUDE_STATE, JSON.stringify(claudeState))
    } catch {
      // 同上
    }
  }, [claudeState])
  useEffect(() => {
    try {
      localStorage.setItem(LS_KEY_CODEX, JSON.stringify(codexState))
    } catch {
      // 同上
    }
  }, [codexState])
  // gateway 视角的全部 model + alias 列表（用于下拉选项）
  const [availableModels, setAvailableModels] = useState<GatewayModelInfo[]>([])
  // codex 直连模式：『模型管理』里已配置的服务商（只列 OpenAI 兼容且启用的）
  const [providers, setProviders] = useState<ProviderView[]>([])
  // claude_code 直连模式：只列 Anthropic 协议且启用的
  const [anthropicProviders, setAnthropicProviders] = useState<ProviderView[]>([])
  // Tauri webview 不支持 window.confirm，统一走自定义对话框（红线见 .windsurf/rules/coding-agent.md 第五条）
  const { confirm, dialog: confirmDialog } = useConfirmDialog()

  // ───────── launcher 子区状态（M5 合并：装好后直接在卡片里启动） ─────────
  // shell 模式跨 row 共享（用户切一次 PowerShell/POSIX 全部 row 同步）
  const [shellMode, setShellMode] = useState<'posix' | 'powershell'>(_loadShellMode)
  useEffect(() => {
    try {
      localStorage.setItem(SHELL_STORAGE_KEY, shellMode)
    } catch {
      /* 静默 */
    }
  }, [shellMode])
  // 每条 launcher 的 cwd（per-id，独立持久化）
  const [cwdMap, setCwdMap] = useState<Record<string, string>>({})
  // launchers 列表变化（首次加载 / gateway 重启）时补齐 cwdMap
  useEffect(() => {
    setCwdMap((prev) => {
      const next = { ...prev }
      launchers.forEach((l) => {
        if (l.runnable && !(l.id in next)) next[l.id] = _loadStoredCwd(l.id)
      })
      return next
    })
  }, [launchers])
  const [launchingId, setLaunchingId] = useState<string | null>(null)
  const [launchToast, setLaunchToast] = useState<{ ok: boolean; text: string } | null>(null)
  const [copiedKey, setCopiedKey] = useState<string | null>(null)

  const setCwdFor = (id: string, cwd: string) => {
    setCwdMap((prev) => ({ ...prev, [id]: cwd }))
    _saveStoredCwd(id, cwd)
  }

  const pickCwdFor = async (id: string) => {
    setLaunchToast(null)
    try {
      const { getRuntime } = await import('../../runtime')
      const runtime = await getRuntime()
      const current = cwdMap[id] || undefined
      const picked = await runtime.dialog.pickDirectory({
        defaultPath: current,
        title: '选择工作区',
      })
      if (picked) setCwdFor(id, picked)
    } catch (e) {
      // pickDirectory 未实现（VSCode webview / 纯浏览器）→ 让用户手动输入
      setLaunchToast({
        ok: false,
        text:
          '当前环境不支持系统目录选择器；请直接在输入框里粘贴绝对路径。详情：' +
          (e instanceof Error ? e.message : String(e)),
      })
      setTimeout(() => setLaunchToast(null), 6000)
    }
  }

  const copyText = (key: string, txt: string) => {
    void navigator.clipboard.writeText(txt)
    setCopiedKey(key)
    setTimeout(() => setCopiedKey((c) => (c === key ? null : c)), 1500)
  }

  const runInTerminal = async (id: string) => {
    setLaunchingId(id)
    setLaunchToast(null)
    try {
      const cwd = (cwdMap[id] || '').trim() || undefined
      const r = await launchGatewayLauncherTerminal(id, shellMode, cwd)
      setLaunchToast({ ok: true, text: r.message || '已启动' })
    } catch (e) {
      setLaunchToast({ ok: false, text: e instanceof Error ? e.message : String(e) })
    } finally {
      setLaunchingId(null)
      setTimeout(() => setLaunchToast(null), 4000)
    }
  }

  const refresh = useCallback(async () => {
    if (!enabled) return
    setLoading(true)
    setError(null)
    try {
      const [data, modelsRes, providersRes] = await Promise.all([
        listGatewayInstallers(),
        listAdminModels('chat').catch(() => ({ object: 'list', data: [] as GatewayModelInfo[] })),
        // 直连模式的服务商下拉；失败不阻塞主流程（仅少了 direct 选项）
        listProviders().catch(() => null),
      ])
      setItems(data)
      setAvailableModels(modelsRes.data || [])
      // codex 只讲 OpenAI 协议：直连下拉只列 openai_chat + enabled 的 provider
      setProviders(
        (providersRes?.providers || []).filter(
          (p) => p.enabled && p.transport === 'openai_chat' && !!p.base_url,
        ),
      )
      // claude_code 只讲 Anthropic 协议：直连下拉只列 anthropic_messages + enabled 的
      setAnthropicProviders(
        (providersRes?.providers || []).filter(
          (p) => p.enabled && p.transport === 'anthropic_messages' && !!p.base_url,
        ),
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [enabled])

  useEffect(() => {
    if (open) void refresh()
  }, [open, refresh])

  const onInstall = async (id: string, force: boolean = false) => {
    setBusyId(id)
    setError(null)
    try {
      // 只 claude_code 携带 defaultModels；其他 installer 后端会忽略此字段
      const defaultModels: ClaudeDefaultModels | undefined =
        id === 'claude_code'
          ? {
              haiku: claudeModels.haiku || null,
              sonnet: claudeModels.sonnet || null,
              opus: claudeModels.opus || null,
            }
          : undefined
      const codex: CodexInstallOptions | undefined =
        id === 'codex'
          ? {
              defaultModel: codexState.defaultModel,
              reasoningEffort: codexState.reasoningEffort,
              mode: codexState.mode,
              directProvider: codexState.mode === 'direct' ? codexState.directProvider : undefined,
            }
          : undefined
      const claude: ClaudeInstallOptions | undefined =
        id === 'claude_code'
          ? {
              mode: claudeState.mode,
              directProvider: claudeState.mode === 'direct' ? claudeState.directProvider : undefined,
            }
          : undefined
      // 若 status 已显示 external_modified，预先让用户确认一次强制覆盖（直接走 force=true）；
      // 否则用户先看到普通流程，仅当 backend 真的返 "外部" 错误时再补一次 confirm（兜底）。
      const knownExternalMod =
        !force && items.find((i) => i.info.id === id)?.status.state === 'external_modified'
      if (knownExternalMod) {
        const ok = await confirm(
          '强制覆盖',
          '该文件被外部改动过（如 codex 运行时写回的内部状态）；强制覆盖会先把当前文件另备份一份再写入。是否继续？',
          'warning',
          '强制覆盖',
        )
        if (!ok) return
        force = true
      }
      const res = await installGatewayInstaller(id, { force, defaultModels, codex, claude })
      if (!res.ok) throw new Error(res.message)
      await refresh()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      // 兜底：status 滞后没显示 external_modified 但 backend 真检测到了
      if (msg.includes('外部') && !force) {
        const ok = await confirm(
          '强制覆盖',
          `${msg}\n\n是否强制覆盖？（会先把当前文件另备份一份再写入）`,
          'warning',
          '强制覆盖',
        )
        if (ok) {
          await onInstall(id, true)
          return
        }
      } else {
        setError(msg)
      }
    } finally {
      setBusyId(null)
    }
  }

  const onUninstall = async (id: string, force: boolean = false) => {
    if (!force) {
      // 已检测到 external_modified 时一步到位弹"强制还原"——避免连续两个 confirm 框
      // （先"卸载 CLI 接入"再"强制还原"）让用户感觉操作没一次完成。
      const knownExternalMod = items.find((i) => i.info.id === id)?.status.state === 'external_modified'
      const ok = await confirm(
        knownExternalMod ? '强制还原' : '卸载 CLI 接入',
        knownExternalMod
          ? '该文件被外部改动过（如 codex 运行时写回的内部状态）；强制还原会丢失这些改动，将文件回滚到最近一次备份。是否继续？'
          : '卸载该 CLI 的接入？将还原到最近一次备份。',
        'danger',
        knownExternalMod ? '强制还原' : '卸载',
      )
      if (!ok) return
      if (knownExternalMod) force = true
    }
    setBusyId(id)
    setError(null)
    try {
      const res = await uninstallGatewayInstaller(id, force)
      if (!res.ok) throw new Error(res.message)
      await refresh()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      // 兜底：status 滞后没显示 external_modified 但 backend 真检测到了
      if (msg.includes('外部') && !force) {
        const ok = await confirm(
          '强制还原',
          `${msg}\n\n是否强制还原？（会丢失你对该文件的手动改动）`,
          'danger',
          '强制还原',
        )
        if (ok) {
          await onUninstall(id, true)
          return
        }
      } else {
        setError(msg)
      }
    } finally {
      setBusyId(null)
    }
  }

  const onPreview = async (id: string) => {
    setBusyId(id)
    setError(null)
    try {
      const defaultModels: ClaudeDefaultModels | undefined =
        id === 'claude_code'
          ? {
              haiku: claudeModels.haiku || null,
              sonnet: claudeModels.sonnet || null,
              opus: claudeModels.opus || null,
            }
          : undefined
      const codex: CodexInstallOptions | undefined =
        id === 'codex'
          ? {
              defaultModel: codexState.defaultModel,
              reasoningEffort: codexState.reasoningEffort,
              mode: codexState.mode,
              directProvider: codexState.mode === 'direct' ? codexState.directProvider : undefined,
            }
          : undefined
      const claude: ClaudeInstallOptions | undefined =
        id === 'claude_code'
          ? {
              mode: claudeState.mode,
              directProvider: claudeState.mode === 'direct' ? claudeState.directProvider : undefined,
            }
          : undefined
      const data = await previewGatewayInstaller(id, { defaultModels, codex, claude })
      setPreview({ id, data })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusyId(null)
    }
  }

  if (!enabled) return null

  const summaryText = (() => {
    if (items.length === 0) return ''
    const us = items.filter((i) => i.status.state === 'installed_pointing_to_us').length
    return `${us} / ${items.length} 已接入`
  })()

  return (
    <section style={cardStyle}>
      <div style={sectionHeadStyle}>
        <button type="button" style={collapseHeadStyle} onClick={() => setOpen((v) => !v)}>
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, marginLeft: 4 }}>
            <Plug size={14} style={{ color: 'var(--primary-color)' }} />
            <span style={{ fontSize: 13.5, fontWeight: 600 }}>客户端接入</span>
            {/* summaryText 折叠/展开都显示，避免标题宽度变化导致跳动 */}
            {summaryText && <span style={countPillStyle}>{summaryText}</span>}
          </span>
        </button>
        <div style={{ display: 'flex', gap: 4, alignItems: 'center', visibility: open ? 'visible' : 'hidden' }}>
          {/* shell 切换：启动命令的 PowerShell/POSIX 选择，跨 row 共享。安装逻辑不受影响 — 仅影响启动与复制。 */}
          <button
            type="button"
            style={shellMode === 'powershell' ? btnPrimarySmallStyle : btnGhostSmallStyle}
            onClick={() => setShellMode('powershell')}
            tabIndex={open ? 0 : -1}
            aria-hidden={!open}
            title="启动/复制命令使用 PowerShell 语法"
          >
            PowerShell
          </button>
          <button
            type="button"
            style={shellMode === 'posix' ? btnPrimarySmallStyle : btnGhostSmallStyle}
            onClick={() => setShellMode('posix')}
            tabIndex={open ? 0 : -1}
            aria-hidden={!open}
            title="启动/复制命令使用 POSIX shell 语法"
          >
            POSIX
          </button>
          <button
            type="button"
            style={iconBtnStyle}
            onClick={() => void refresh()}
            disabled={loading || !open}
            tabIndex={open ? 0 : -1}
            aria-hidden={!open}
            title="刷新接入状态"
          >
            {loading ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />}
          </button>
        </div>
      </div>

      {open && (
        <>
          {error && (
            <div style={errBoxStyle}>
              <AlertCircle size={13} />
              <span style={{ flex: 1 }}>{error}</span>
              <button type="button" style={btnGhostStyle} onClick={() => setError(null)}>×</button>
            </div>
          )}

          {items.map((it) => {
            const launcher = launchers.find((l) => l.id === it.info.id) || null
            return (
              <InstallerRow
                key={it.info.id}
                entry={it}
                busy={busyId === it.info.id}
                availableModels={availableModels}
                providers={providers}
                anthropicProviders={anthropicProviders}
                claudeModels={claudeModels}
                claudeState={claudeState}
                onClaudeStateChange={setClaudeState}
                onClaudeModelsChange={setClaudeModels}
                codexState={codexState}
                onCodexStateChange={setCodexState}
                onInstall={() => onInstall(it.info.id)}
                onUninstall={() => onUninstall(it.info.id)}
                onPreview={() => onPreview(it.info.id)}
                launcher={launcher}
                shellMode={shellMode}
                cwd={cwdMap[it.info.id] || ''}
                onCwdChange={(v) => setCwdFor(it.info.id, v)}
                onPickCwd={() => void pickCwdFor(it.info.id)}
                launching={launchingId === it.info.id}
                onLaunch={() => void runInTerminal(it.info.id)}
                onCopyCommand={(cmd) => copyText(`l:${it.info.id}`, cmd)}
                copied={copiedKey === `l:${it.info.id}`}
              />
            )
          })}

          {items.length > 0 && (
            <div style={footHintStyle}>写入前自动备份原配置，卸载可一键还原。</div>
          )}

          {launchToast && (
            <div style={launchToast.ok ? toastOkStyle : toastErrStyle}>{launchToast.text}</div>
          )}

          {preview && (
            <PreviewModal
              data={preview.data}
              installerName={items.find((i) => i.info.id === preview.id)?.info.name || preview.id}
              onClose={() => setPreview(null)}
            />
          )}
        </>
      )}
      {confirmDialog}
    </section>
  )
}

/* ─────────── 单 installer 行 ─────────── */

function InstallerRow({
  entry,
  busy,
  availableModels,
  providers,
  anthropicProviders,
  claudeModels,
  claudeState,
  onClaudeStateChange,
  onClaudeModelsChange,
  codexState,
  onCodexStateChange,
  onInstall,
  onUninstall,
  onPreview,
  launcher,
  shellMode,
  cwd,
  onCwdChange,
  onPickCwd,
  launching,
  onLaunch,
  onCopyCommand,
  copied,
}: {
  entry: GatewayInstallerEntry
  busy: boolean
  availableModels: GatewayModelInfo[]
  providers: ProviderView[]
  anthropicProviders: ProviderView[]
  claudeModels: DefaultModelsState
  claudeState: ClaudeState
  onClaudeStateChange: (v: ClaudeState) => void
  onClaudeModelsChange: (v: DefaultModelsState) => void
  codexState: CodexState
  onCodexStateChange: (v: CodexState) => void
  onInstall: () => void
  onUninstall: () => void
  onPreview: () => void
  launcher: GatewayLauncherSnippet | null
  shellMode: 'posix' | 'powershell'
  cwd: string
  onCwdChange: (v: string) => void
  onPickCwd: () => void
  launching: boolean
  onLaunch: () => void
  onCopyCommand: (cmd: string) => void
  copied: boolean
}) {
  const { info, status } = entry
  const installed = status.state === 'installed_pointing_to_us'
  const elsewhere = status.state === 'installed_pointing_elsewhere'
  const externalMod = status.state === 'external_modified'
  const isClaude = info.id === 'claude_code'
  const isCodex = info.id === 'codex'
  // codex 必填 default_model，未选 → 接入禁用；直连模式还需选服务提供商
  const codexNeedsModel =
    isCodex &&
    !installed &&
    (!codexState.defaultModel ||
      (codexState.mode === 'direct' && !codexState.directProvider))
  // claude 直连模式必须选服务商才可接入
  const claudeNeedsProvider =
    isClaude && !installed && claudeState.mode === 'direct' && !claudeState.directProvider
  const installDisabled = busy || codexNeedsModel || claudeNeedsProvider
  const showLauncherBlock = !!launcher && launcher.runnable
  const launcherCmd = launcher
    ? shellMode === 'powershell'
      ? launcher.powershell_command
      : launcher.posix_command
    : ''
  // 选项默认展开：配置项本就少，藏一层反而每次都要多点一下
  const [optionsOpen, setOptionsOpen] = useState(true)
  const [notesOpen, setNotesOpen] = useState(false)
  const hasOptions = isClaude || isCodex
  const needsAttention = (codexNeedsModel || claudeNeedsProvider) && !optionsOpen
  // 上次接入时间 / 备份数收进状态徽标的 tooltip，不再占行
  const statusTip = [
    status.last_install_at ? `上次接入 ${formatTs(status.last_install_at)}` : '',
    status.backup_count > 0 ? `${status.backup_count} 个备份` : '',
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <div style={rowStyle}>
      <div style={rowHeadStyle}>
        <div style={rowTitleStyle}>
          <strong style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-color)' }}>{info.name}</strong>
          <span title={statusTip || undefined}>
            <StatusBadge state={status.state} />
          </span>
          <code style={rowPathStyle} title={info.target_path}>
            {info.target_path}
          </code>
        </div>
        <div style={rowActionsStyle}>
          <button
            type="button"
            style={iconBtnStyle}
            onClick={onPreview}
            disabled={busy}
            title="预览将要写入的内容（不实际修改）"
          >
            <Eye size={13} />
          </button>
          {!installed ? (
            <button
              type="button"
              style={btnPrimaryStyle}
              onClick={onInstall}
              disabled={installDisabled}
              title={
                codexNeedsModel
                  ? '请先在「选项」里选一个默认模型'
                  : claudeNeedsProvider
                    ? '直连模式需先在「选项」里选服务商'
                    : '写入配置（原文件自动备份）'
              }
            >
              {busy ? <Loader2 size={12} className="spin" /> : <PlugZap size={12} />}
              <span style={{ marginLeft: 4 }}>{busy ? '处理中' : '接入'}</span>
            </button>
          ) : (
            <button
              type="button"
              style={btnDangerStyle}
              onClick={onUninstall}
              disabled={busy}
              title="卸载并还原最近一次备份"
            >
              {busy ? <Loader2 size={12} className="spin" /> : <Trash2 size={12} />}
              <span style={{ marginLeft: 4 }}>{busy ? '处理中' : '卸载'}</span>
            </button>
          )}
        </div>
      </div>
      {(elsewhere || externalMod) && <div style={subWarn}>{status.detail}</div>}

      {hasOptions && (
        <div>
          <button type="button" style={optionsToggleStyle} onClick={() => setOptionsOpen((v) => !v)}>
            {optionsOpen ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            <span style={{ marginLeft: 3 }}>选项</span>
            {needsAttention && <span style={needDotStyle} title="有待填写的必填项" />}
          </button>
          {optionsOpen && (
            <div style={optionsBoxStyle}>
              {isClaude && (
                <ClaudeOptions
                  providers={anthropicProviders}
                  state={claudeState}
                  onStateChange={onClaudeStateChange}
                  models={claudeModels}
                  onModelsChange={onClaudeModelsChange}
                  availableModels={availableModels}
                  disabled={busy}
                />
              )}
              {isCodex && (
                <CodexOptions
                  providers={providers}
                  state={codexState}
                  onStateChange={onCodexStateChange}
                  availableModels={availableModels}
                  disabled={busy}
                />
              )}
            </div>
          )}
        </div>
      )}

      {/* 启动条：单行命令（过长省略，hover 看全文）+ 启动 / 复制 + 工作区。与接入状态解耦——
          未接入时也能复制 / 启动，CLI 没读到配置会回落到它自己的默认行为。 */}
      {showLauncherBlock && launcher && (
        <div style={launcherStyle}>
          <div style={cmdRowStyle}>
            <code style={cmdLineStyle} title={launcherCmd}>
              {launcherCmd}
            </code>
            <button
              type="button"
              style={iconBtnPrimaryStyle}
              onClick={onLaunch}
              disabled={launching}
              title="在新终端启动"
            >
              {launching ? <Loader2 size={12} className="spin" /> : <Play size={12} />}
            </button>
            <button
              type="button"
              style={iconBtnStyle}
              onClick={() => onCopyCommand(launcherCmd)}
              title={copied ? '已复制' : '复制命令'}
            >
              {copied ? <CheckCircle2 size={12} /> : <Copy size={12} />}
            </button>
          </div>
          <div style={cmdRowStyle}>
            <input
              type="text"
              value={cwd}
              onChange={(e) => onCwdChange(e.target.value)}
              placeholder="工作区目录（可选，留空用默认）"
              style={cwdInputStyle}
              spellCheck={false}
            />
            <button type="button" style={iconBtnStyle} onClick={onPickCwd} title="选择目录">
              <FolderOpen size={12} />
            </button>
            {cwd && (
              <button type="button" style={iconBtnStyle} onClick={() => onCwdChange('')} title="清除">
                <X size={12} />
              </button>
            )}
            {launcher.notes.length > 0 && (
              <button type="button" style={optionsToggleStyle} onClick={() => setNotesOpen((v) => !v)}>
                {notesOpen ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                <span style={{ marginLeft: 3 }}>说明</span>
              </button>
            )}
          </div>
          {notesOpen && (
            <ul style={launcherNotesStyle}>
              {launcher.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

/** Codex 选项：接入模式（+直连服务商）、默认模型（必填）、推理强度。 */
function CodexOptions({
  providers,
  state,
  onStateChange,
  availableModels,
  disabled,
}: {
  providers: ProviderView[]
  state: CodexState
  onStateChange: (v: CodexState) => void
  availableModels: GatewayModelInfo[]
  disabled: boolean
}) {
  const aliases = availableModels.filter((m) => m.transport === 'alias')
  const direct = state.mode === 'direct'
  // 直连时只列所选 provider 的 model（model 必须是 vendor 裸 id，
  // 前端仍发 namespaced id，后端负责剥前缀）；网关模式列全部 + 别名
  const concrete = availableModels.filter(
    (m) => m.transport !== 'alias' && (!direct || m.provider === state.directProvider),
  )
  const needSelect = !state.defaultModel
  const needProvider = direct && !state.directProvider
  // 直连选中了 provider 但没配 api_key → 允许安装（部分本地端点不鉴权），仅提示
  const selectedProvider = providers.find((p) => p.id === state.directProvider)

  return (
    <>
      <div style={optRowStyle}>
        <span
          style={optLabelStyle}
          title="网关：经柳如烟本地转发（支持别名 / fallback / 用量统计）；直连：服务商地址与密钥直接写进 codex 配置，不经转发"
        >
          接入模式
        </span>
        <select
          value={state.mode}
          onChange={(e) => onStateChange({ ...state, mode: e.target.value as CodexState['mode'] })}
          disabled={disabled}
          style={{ ...selectStyle, width: 150, flex: '0 0 auto' }}
        >
          <option value="gateway">网关（本地转发）</option>
          <option value="direct" disabled={providers.length === 0}>
            直连服务商{providers.length === 0 ? '（无可用 OpenAI 服务商）' : ''}
          </option>
        </select>
        {direct && (
          <>
            <span style={optLabelStyle}>服务商</span>
            <select
              value={state.directProvider}
              onChange={(e) => onStateChange({ ...state, directProvider: e.target.value })}
              disabled={disabled || providers.length === 0}
              style={{
                ...selectStyle,
                flex: '1 1 180px',
                minWidth: 160,
                ...(needProvider ? { border: '1px solid #e5484d' } : {}),
              }}
            >
              <option value="">— 请选择 —</option>
              {providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name || p.id}{p.has_api_key ? '' : '（未配 API Key）'}
                </option>
              ))}
            </select>
          </>
        )}
      </div>
      {direct && selectedProvider && !selectedProvider.has_api_key && (
        <div style={warnTinyStyle}>该服务商未配 API Key，仅免鉴权端点可用</div>
      )}
      <div style={optRowStyle}>
        <span
          style={optLabelStyle}
          title="必填：不选 codex 会用它自己的默认（gpt-5-codex）导致上游 404"
        >
          默认模型
        </span>
        <select
          value={state.defaultModel}
          onChange={(e) => onStateChange({ ...state, defaultModel: e.target.value })}
          disabled={disabled || availableModels.length === 0 || (direct && !state.directProvider)}
          style={{
            ...selectStyle,
            flex: '1 1 200px',
            minWidth: 180,
            ...(needSelect ? { border: '1px solid #e5484d' } : {}),
          }}
        >
          <option value="">— 必选 —</option>
          {!direct && aliases.length > 0 && (
            <optgroup label="别名">
              {aliases.map((m) => (
                <option key={m.id} value={m.id}>{m.id}</option>
              ))}
            </optgroup>
          )}
          {concrete.length > 0 && (
            <optgroup label={direct ? '该服务商的模型' : '模型'}>
              {concrete.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}{m.provider_name ? ` (${m.provider_name})` : ''}
                </option>
              ))}
            </optgroup>
          )}
        </select>
        <span style={optLabelStyle} title="推理强度">推理</span>
        <select
          value={state.reasoningEffort}
          onChange={(e) =>
            onStateChange({
              ...state,
              reasoningEffort: e.target.value as CodexState['reasoningEffort'],
            })
          }
          disabled={disabled}
          style={{ ...selectStyle, width: 92, flex: '0 0 auto' }}
        >
          <option value="minimal">minimal</option>
          <option value="low">low</option>
          <option value="medium">medium</option>
          <option value="high">high</option>
        </select>
      </div>
    </>
  )
}

/** Claude Code 选项：接入模式（+直连服务商）、haiku/sonnet/opus 三角色模型分配。 */
function ClaudeOptions({
  providers,
  state,
  onStateChange,
  models,
  onModelsChange,
  availableModels,
  disabled,
}: {
  providers: ProviderView[]
  state: ClaudeState
  onStateChange: (v: ClaudeState) => void
  models: DefaultModelsState
  onModelsChange: (v: DefaultModelsState) => void
  availableModels: GatewayModelInfo[]
  disabled: boolean
}) {
  const direct = state.mode === 'direct'
  const needProvider = direct && !state.directProvider
  const selectedProvider = providers.find((p) => p.id === state.directProvider)
  // 直连只列所选 provider 的模型（值仍发 namespaced id，后端剥前缀），且不列别名（别名只在网关侧解析）
  const aliases = availableModels.filter((m) => m.transport === 'alias')
  const concrete = availableModels.filter(
    (m) => m.transport !== 'alias' && (!direct || m.provider === state.directProvider),
  )
  const modelsDisabled = disabled || availableModels.length === 0 || (direct && !state.directProvider)

  return (
    <>
      <div style={optRowStyle}>
        <span
          style={optLabelStyle}
          title="网关：经柳如烟本地转发（支持别名 / fallback / 协议互转 / 用量统计）；直连：服务商地址与密钥直接写进 Claude 配置，不经转发（仅支持 Anthropic 协议服务商）"
        >
          接入模式
        </span>
        <select
          value={state.mode}
          onChange={(e) => onStateChange({ ...state, mode: e.target.value as ClaudeState['mode'] })}
          disabled={disabled}
          style={{ ...selectStyle, width: 150, flex: '0 0 auto' }}
        >
          <option value="gateway">网关（本地转发）</option>
          <option value="direct" disabled={providers.length === 0}>
            直连服务商{providers.length === 0 ? '（无可用 Anthropic 服务商）' : ''}
          </option>
        </select>
        {direct && (
          <>
            <span style={optLabelStyle}>服务商</span>
            <select
              value={state.directProvider}
              onChange={(e) => onStateChange({ ...state, directProvider: e.target.value })}
              disabled={disabled || providers.length === 0}
              style={{
                ...selectStyle,
                flex: '1 1 180px',
                minWidth: 160,
                ...(needProvider ? { border: '1px solid #e5484d' } : {}),
              }}
            >
              <option value="">— 请选择 —</option>
              {providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name || p.id}{p.has_api_key ? '' : '（未配 API Key）'}
                </option>
              ))}
            </select>
          </>
        )}
      </div>
      {direct && selectedProvider && !selectedProvider.has_api_key && (
        <div style={warnTinyStyle}>该服务商未配 API Key，仅免鉴权端点可用</div>
      )}
      <div
        style={optRowStyle}
        title="分别映射 Claude Code 内置的轻量 / 默认 / 复杂三角色；不指定则用 Claude 内置默认模型"
      >
        <span style={optLabelStyle}>模型分配</span>
        {(['haiku', 'sonnet', 'opus'] as const).map((role) => (
          <select
            key={role}
            value={models[role]}
            onChange={(e) => onModelsChange({ ...models, [role]: e.target.value })}
            disabled={modelsDisabled}
            style={{ ...selectStyle, flex: '1 1 140px', minWidth: 130 }}
            title={`${role} 角色`}
          >
            <option value="">{role} · 默认</option>
            {!direct && aliases.length > 0 && (
              <optgroup label="别名">
                {aliases.map((m) => (
                  <option key={m.id} value={m.id}>{m.id}</option>
                ))}
              </optgroup>
            )}
            {concrete.length > 0 && (
              <optgroup label={direct ? '该服务商的模型' : '模型'}>
                {concrete.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id}{m.provider_name ? ` (${m.provider_name})` : ''}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        ))}
      </div>
    </>
  )
}

function StatusBadge({ state }: { state: GatewayInstallerState }) {
  const styles: Record<GatewayInstallerState, { bg: string; color: string; label: string; icon: React.ReactNode }> = {
    not_installed: {
      bg: 'rgba(0,0,0,0.05)',
      color: 'var(--text-tertiary, #888)',
      label: '未接入',
      icon: <Plug size={10} />,
    },
    installed_pointing_to_us: {
      bg: 'rgba(46,160,67,0.12)',
      color: '#2ea043',
      label: '已接入',
      icon: <CheckCircle2 size={10} />,
    },
    installed_pointing_elsewhere: {
      bg: 'rgba(245,159,0,0.12)',
      color: '#d29922',
      label: '指向其他',
      icon: <AlertCircle size={10} />,
    },
    external_modified: {
      bg: 'rgba(229,72,77,0.12)',
      color: '#e5484d',
      label: '外部改动过',
      icon: <AlertCircle size={10} />,
    },
    error: {
      bg: 'rgba(229,72,77,0.12)',
      color: '#e5484d',
      label: '错误',
      icon: <AlertCircle size={10} />,
    },
  }
  const s = styles[state]
  return (
    <span style={{ ...badgeStyle, background: s.bg, color: s.color }}>
      {s.icon}
      <span style={{ marginLeft: 3 }}>{s.label}</span>
    </span>
  )
}

/* ─────────── 预览 modal ─────────── */

function PreviewModal({
  data,
  installerName,
  onClose,
}: {
  data: GatewayInstallerPreview
  installerName: string
  onClose: () => void
}) {
  return (
    <div
      style={modalBgStyle}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div style={modalCardStyle}>
        <div style={modalHeadStyle}>
          <span>预览：{installerName} 接入</span>
          <button type="button" style={btnGhostStyle} onClick={onClose}>×</button>
        </div>
        <div style={{ fontSize: 11.5, color: 'var(--text-secondary)', marginBottom: 8, lineHeight: 1.7 }}>
          目标文件：<code style={codeStyle}>{data.target_path}</code>
          <br />
          {data.will_create && <span>📄 将<strong>新建</strong>文件</span>}
          {data.will_modify && <span>✏️ 将<strong>修改</strong>现有文件</span>}
          {!data.will_create && !data.will_modify && <span>✅ 文件已是目标内容（无变化）</span>}
          {data.will_backup_to && (
            <>
              <br />
              💾 备份至：<code style={codeStyle}>{data.will_backup_to}</code>
            </>
          )}
          {data.external_modified && (
            <>
              <br />
              <span style={{ color: '#e5484d' }}>
                ⚠ 文件被外部改动过；正式 install 时会要求确认
              </span>
            </>
          )}
        </div>
        <pre style={diffPreStyle}>{data.diff_summary}</pre>
      </div>
    </div>
  )
}

/* ─────────── helpers ─────────── */

function formatTs(ts: string): string {
  if (!ts || ts.length < 16) return ts
  return ts.slice(0, 16).replace('T', ' ')
}

/* ─────────── styles ─────────── */

const cardStyle: React.CSSProperties = {
  background: 'var(--card-bg, rgba(0,0,0,0.02))',
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  borderRadius: 12,
  padding: '14px 18px',
  boxShadow: '0 1px 2px rgba(0,0,0,0.04), 0 8px 24px -12px rgba(0,0,0,0.10)',
}

const sectionHeadStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  fontSize: 12.5,
  fontWeight: 600,
  color: 'var(--text-color)',
  marginBottom: 8,
  gap: 8,
  // 不 wrap：避免窄宽下"刷新"按钮换行让标题行高度跳动
  flexWrap: 'nowrap',
  minHeight: 24,  // 锁高度，刷新按钮 visibility 切换不会让整行高度跳
}

const collapseHeadStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  background: 'transparent',
  border: 'none',
  cursor: 'pointer',
  padding: 0,
  font: 'inherit',
  color: 'inherit',
  flex: 1,           // 占据剩余空间
  minWidth: 0,       // 配合 flexWrap:nowrap，让标题文字可以收缩，不会把刷新按钮挤出去
  textAlign: 'left',
}

const btnBaseStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  padding: '4px 10px',
  border: '1px solid var(--border-color, rgba(0,0,0,0.12))',
  borderRadius: 8,
  background: 'var(--card-bg, transparent)',
  fontSize: 11.5,
  cursor: 'pointer',
  color: 'var(--text-color)',
}

const btnGhostStyle: React.CSSProperties = { ...btnBaseStyle }
const btnPrimaryStyle: React.CSSProperties = {
  ...btnBaseStyle,
  background: 'var(--primary-color, #5e6ad2)',
  color: '#fff',
  borderColor: 'transparent',
}
const btnDangerStyle: React.CSSProperties = {
  ...btnBaseStyle,
  borderColor: '#e5484d',
  color: '#e5484d',
}
// Small variants 给标题区 shell 切换用——避免占用过多空间
const btnGhostSmallStyle: React.CSSProperties = { ...btnGhostStyle, padding: '3px 8px', fontSize: 11 }
const btnPrimarySmallStyle: React.CSSProperties = { ...btnPrimaryStyle, padding: '3px 8px', fontSize: 11 }

const rowStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 6,
  padding: '12px 14px',
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  borderRadius: 9,
  marginBottom: 8,
  background: 'var(--bg-secondary, rgba(0,0,0,0.015))',
}

const rowHeadStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 8,
  // 窄窗口时操作按钮换到标题下一行，而不是把行撑宽 / 裁掉
  flexWrap: 'wrap',
}

const rowTitleStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  flex: 1,
  minWidth: 0,
  flexWrap: 'wrap',
}

const rowPathStyle: React.CSSProperties = {
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 10,
  color: 'var(--text-tertiary, #888)',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  maxWidth: 340,
}

const rowActionsStyle: React.CSSProperties = {
  display: 'flex',
  gap: 4,
  alignItems: 'center',
  flexShrink: 0,
}

const optionsToggleStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  background: 'transparent',
  border: 'none',
  cursor: 'pointer',
  padding: '2px 0',
  fontSize: 11,
  color: 'var(--text-tertiary, #888)',
}

const needDotStyle: React.CSSProperties = {
  width: 5,
  height: 5,
  borderRadius: '50%',
  background: '#e5484d',
  marginLeft: 5,
}

const optionsBoxStyle: React.CSSProperties = {
  marginTop: 4,
  paddingTop: 8,
  borderTop: '1px solid var(--border-color, rgba(0,0,0,0.07))',
  display: 'flex',
  flexDirection: 'column',
  gap: 6,
}

const optRowStyle: React.CSSProperties = {
  display: 'flex',
  gap: 8,
  alignItems: 'center',
  flexWrap: 'wrap',
}

const optLabelStyle: React.CSSProperties = {
  fontSize: 11,
  color: 'var(--text-secondary)',
  flexShrink: 0,
}

const warnTinyStyle: React.CSSProperties = {
  fontSize: 10.5,
  color: '#e5a13a',
}

const selectStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  fontSize: 11.5,
  padding: '5px 8px',
  border: '1px solid #555',
  borderRadius: 6,
  background: 'var(--bg-color, #fff)',
  color: 'var(--text-color)',
  outline: 'none',
}

const badgeStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  fontSize: 10,
  padding: '1px 7px',
  borderRadius: 999,
  fontWeight: 500,
}

const countPillStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono, ui-monospace, Consolas, monospace)',
  fontSize: 10.5,
  fontWeight: 400,
  color: 'var(--primary-color)',
  background: 'var(--accent-bg, rgba(196,167,125,0.12))',
  borderRadius: 999,
  padding: '1px 8px',
}

const codeStyle: React.CSSProperties = {
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 10.5,
  background: 'rgba(0,0,0,0.05)',
  padding: '1px 5px',
  borderRadius: 3,
}

const footHintStyle: React.CSSProperties = {
  marginTop: 2,
  fontSize: 10.5,
  color: 'var(--text-tertiary, #888)',
}

const subWarn: React.CSSProperties = {
  fontSize: 11,
  color: '#d29922',
}

/* ─── launcher 子区 styles ─── */

const launcherStyle: React.CSSProperties = {
  paddingTop: 8,
  borderTop: '1px solid var(--border-color, rgba(0,0,0,0.07))',
  display: 'flex',
  flexDirection: 'column',
  gap: 6,
}

const cmdRowStyle: React.CSSProperties = {
  display: 'flex',
  gap: 4,
  alignItems: 'center',
  minWidth: 0,
  flexWrap: 'wrap',
  // 命令是 nowrap 单行（省略号截断），其 min-content = 全文宽，会沿 intrinsic sizing
  // 逐层撑宽祖先 flex 链（实测把 settings-view 撑到 1206px 被外层裁掉）。
  // inline-size  containment 让本行内在宽度与内容脱钩，只跟父级走。
  containerType: 'inline-size',
}

const cmdLineStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 11,
  padding: '6px 10px',
  background: 'rgba(0,0,0,0.04)',
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  borderRadius: 6,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  color: 'var(--text-color)',
}

const iconBtnStyle: React.CSSProperties = { ...btnBaseStyle, padding: '4px 6px' }
const iconBtnPrimaryStyle: React.CSSProperties = { ...btnPrimaryStyle, padding: '4px 6px' }

const launcherNotesStyle: React.CSSProperties = {
  margin: 0,
  paddingLeft: 16,
  fontSize: 11,
  color: 'var(--text-secondary)',
  lineHeight: 1.65,
}

const cwdInputStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 11,
  padding: '5px 8px',
  border: '1px solid var(--border-color, rgba(0,0,0,0.15))',
  borderRadius: 6,
  background: 'var(--bg-color, transparent)',
  color: 'var(--text-color)',
}

const toastOkStyle: React.CSSProperties = {
  marginTop: 8,
  padding: '6px 10px',
  borderRadius: 5,
  fontSize: 11.5,
  background: 'rgba(80, 170, 90, 0.12)',
  color: '#2c8f3c',
  border: '1px solid rgba(80,170,90,0.4)',
}

const toastErrStyle: React.CSSProperties = {
  marginTop: 8,
  padding: '6px 10px',
  borderRadius: 5,
  fontSize: 11.5,
  background: 'rgba(229, 72, 77, 0.12)',
  color: '#c83235',
  border: '1px solid rgba(229,72,77,0.4)',
}

const errBoxStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  padding: 8,
  fontSize: 11.5,
  color: '#e5484d',
  background: 'rgba(229,72,77,0.08)',
  borderRadius: 6,
  marginBottom: 8,
}

const modalBgStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(0,0,0,0.4)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 9999,
  padding: 16,
}

const modalCardStyle: React.CSSProperties = {
  background: 'var(--bg-color, #fff)',
  border: '1px solid var(--border-color, rgba(0,0,0,0.12))',
  borderRadius: 8,
  padding: 16,
  width: 'min(720px, 100%)',
  maxHeight: '80vh',
  display: 'flex',
  flexDirection: 'column',
}

const modalHeadStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  fontSize: 13,
  fontWeight: 600,
  marginBottom: 10,
  color: 'var(--text-color)',
}

const diffPreStyle: React.CSSProperties = {
  flex: 1,
  margin: 0,
  padding: 10,
  background: 'rgba(0,0,0,0.04)',
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  borderRadius: 4,
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 11,
  whiteSpace: 'pre-wrap',
  overflow: 'auto',
  lineHeight: 1.5,
  color: 'var(--text-color)',
}
