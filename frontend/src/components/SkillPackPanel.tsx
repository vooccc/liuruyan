/**
 * Skill Pack Marketplace UI（柳如烟兼容 Anthropic Claude Code Plugin 协议）。
 *
 * 形态：嵌在 SkillPanel 顶部 viewMode 切换的第三个标签页。
 * 主要交互：
 *   - 查看已安装的 pack 列表（版本、来源、venv 状态）
 *   - 输入 GitHub repo（owner/repo 或完整 URL）一键安装
 *   - 升级 / 卸载 / 详情
 *
 * 后端：backend/main.py /skill-packs 系列路由
 * 设计文档：docs/skill_marketplace.md
 *
 * 复用现有 .panel-* 与 .skill-* 全局类，避免引入新 CSS 文件——风格与
 * SkillPanel / MCPSettings 保持一致。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Download,
  ExternalLink,
  Github,
  Info,
  Loader2,
  Package,
  Plus,
  RefreshCw,
  Trash2,
  Upload,
  X,
} from 'lucide-react'
import {
  installSkillPack,
  listSkillPacks,
  uninstallSkillPack,
  updateSkillPack,
  type SkillPackInstalled,
} from '../api/skillPack'
import { useConfirmDialog } from './ConfirmDialog'

// ────────────────────────────────────────────────────────────────────────────
// 推荐 pack 内置目录
// ────────────────────────────────────────────────────────────────────────────
// 当 marketplace 目录服务还没起来前先用硬编码的"精选清单"。后续可改为
// 拉远程 catalog（参考 MCPSettings 的 MarketplaceSettings 订阅 URL 思路）。

interface FeaturedPack {
  source: string // owner/repo
  display_name: string
  category: string
  tagline: string
  install_size_hint?: string // e.g. '~500MB'，给用户心理预期
}

const FEATURED_PACKS: FeaturedPack[] = [
  {
    source: 'lingfengQAQ/webnovel-writer',
    display_name: '网文创作系统',
    category: '写作',
    tagline: '长篇网文创作（skills + agents + data chain + RAG）',
    install_size_hint: '~600MB（含向量模型）',
  },
]

// ────────────────────────────────────────────────────────────────────────────
// 工具函数
// ────────────────────────────────────────────────────────────────────────────

function formatSource(source: Record<string, unknown> | undefined | null): string {
  if (!source) return '本地'
  if (source.source === 'github' && typeof source.repo === 'string') return source.repo
  if (typeof source.url === 'string') return source.url
  return JSON.stringify(source)
}

function formatTime(ts: number | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleString()
}

// ────────────────────────────────────────────────────────────────────────────
// 主组件
// ────────────────────────────────────────────────────────────────────────────

export default function SkillPackPanel() {
  const [packs, setPacks] = useState<SkillPackInstalled[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busyPack, setBusyPack] = useState<string | null>(null)
  const [busyAction, setBusyAction] = useState<'install' | 'update' | 'uninstall' | null>(null)
  const [installSourceInput, setInstallSourceInput] = useState('')
  const [installCreateVenv, setInstallCreateVenv] = useState(true)
  const [installInstallDeps, setInstallInstallDeps] = useState(true)
  const [installResult, setInstallResult] = useState<string | null>(null)
  const { confirm, dialog: confirmDialog } = useConfirmDialog()

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const list = await listSkillPacks()
      setPacks(list)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const installedNames = useMemo(() => new Set(packs.map((p) => p.pack_name)), [packs])

  // ─── 安装 ──────────────────────────────────────────────────────────────────

  const handleInstall = useCallback(
    async (
      source: string,
      opts?: { create_venv?: boolean; install_deps?: boolean; overwrite?: boolean },
    ) => {
      const trimmed = source.trim()
      if (!trimmed) {
        setError('请输入 GitHub 仓库（owner/repo 或完整 URL）')
        return
      }
      setBusyPack(trimmed)
      setBusyAction('install')
      setError(null)
      setInstallResult(null)
      try {
        const r = await installSkillPack({
          source: trimmed,
          create_venv: opts?.create_venv ?? installCreateVenv,
          install_deps: opts?.install_deps ?? installInstallDeps,
          overwrite: opts?.overwrite ?? false,
        })
        setInstallResult(
          `✓ 安装成功：${r.plugin_name}@${r.plugin_version}（${r.skills_count} 个 skill）` +
            (r.venv_created ? '；子 venv 已创建' : '；未创建子 venv') +
            (r.deps_installed ? '；依赖已安装' : ''),
        )
        setInstallSourceInput('')
        await refresh()
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        // 检测"目标目录已存在"，给一键覆盖按钮
        if (/已存在/.test(msg) && !opts?.overwrite) {
          const yes = await confirm(
            '该 pack 已安装',
            '是否强制覆盖重装？这会删除原有目录并重新 clone。',
            'danger',
            '覆盖重装',
          )
          if (yes) {
            await handleInstall(trimmed, { ...opts, overwrite: true })
            return
          }
        }
        setError(msg)
      } finally {
        setBusyPack(null)
        setBusyAction(null)
      }
    },
    [confirm, installCreateVenv, installInstallDeps, refresh],
  )

  // ─── 升级 ──────────────────────────────────────────────────────────────────

  const handleUpdate = useCallback(
    async (packName: string) => {
      setBusyPack(packName)
      setBusyAction('update')
      setError(null)
      setInstallResult(null)
      try {
        const r = await updateSkillPack(packName)
        if (r.changed) {
          setInstallResult(
            `✓ ${packName} 已升级：${r.before_version ?? '?'} → ${r.after_version ?? '?'}`,
          )
        } else {
          setInstallResult(`已是最新版本（${r.after_version ?? r.before_version ?? '?'}）`)
        }
        await refresh()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setBusyPack(null)
        setBusyAction(null)
      }
    },
    [refresh],
  )

  // ─── 卸载 ──────────────────────────────────────────────────────────────────

  const handleUninstall = useCallback(
    async (packName: string) => {
      const yes = await confirm(
        `卸载 ${packName}?`,
        '会删除 pack 目录和子 venv。已装备此 pack 的角色会自动失去对应技能。',
        'danger',
        '卸载',
      )
      if (!yes) return
      setBusyPack(packName)
      setBusyAction('uninstall')
      setError(null)
      setInstallResult(null)
      try {
        await uninstallSkillPack(packName)
        setInstallResult(`✓ 已卸载 ${packName}`)
        await refresh()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setBusyPack(null)
        setBusyAction(null)
      }
    },
    [confirm, refresh],
  )

  // ─── 渲染 ──────────────────────────────────────────────────────────────────

  return (
    <div className="skill-pack-panel">
      {/* ===== 顶部说明 + 刷新 ===== */}
      <div className="skill-section-header">
        <span>
          <Package size={14} /> 插件包管理
        </span>
        <button
          className="icon-btn"
          onClick={() => void refresh()}
          disabled={loading || busyAction !== null}
          title="刷新列表"
        >
          <RefreshCw size={14} className={loading ? 'spin' : ''} />
        </button>
      </div>

      <p className="skill-pack-hint">
        从 GitHub 安装 Anthropic Claude Code Plugin 兼容的技能包。每个 pack 独立 venv，
        不污染主环境；安装后里面的 skill 会自动出现在「全局技能池」（带{' '}
        <code>&lt;pack&gt;__</code> 前缀），可像普通 skill 一样装备给角色。
      </p>

      {/* ===== 错误 / 成功提示 ===== */}
      {error && (
        <div className="skill-pack-banner skill-pack-banner--error">
          <AlertCircle size={14} /> {error}
          <button className="skill-pack-banner-close" onClick={() => setError(null)}>
            <X size={12} />
          </button>
        </div>
      )}
      {installResult && (
        <div className="skill-pack-banner skill-pack-banner--ok">
          <CheckCircle2 size={14} /> {installResult}
          <button className="skill-pack-banner-close" onClick={() => setInstallResult(null)}>
            <X size={12} />
          </button>
        </div>
      )}

      {/* ===== 安装表单 ===== */}
      <div className="skill-pack-install-form">
        <div className="skill-pack-form-row">
          <Github size={16} />
          <input
            type="text"
            placeholder="owner/repo  或  https://github.com/owner/repo.git"
            value={installSourceInput}
            onChange={(e) => setInstallSourceInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && installSourceInput.trim() && busyAction === null) {
                e.preventDefault()
                void handleInstall(installSourceInput)
              }
            }}
            disabled={busyAction === 'install'}
          />
          <button
            className="skill-pack-btn-primary"
            disabled={busyAction !== null || !installSourceInput.trim()}
            onClick={() => void handleInstall(installSourceInput)}
          >
            {busyAction === 'install' ? (
              <>
                <Loader2 size={14} className="spin" /> 安装中…
              </>
            ) : (
              <>
                <Download size={14} /> 安装
              </>
            )}
          </button>
        </div>

        <div className="skill-pack-form-options">
          <label>
            <input
              type="checkbox"
              checked={installCreateVenv}
              onChange={(e) => setInstallCreateVenv(e.target.checked)}
              disabled={busyAction === 'install'}
            />
            创建子 venv（推荐）
          </label>
          <label>
            <input
              type="checkbox"
              checked={installInstallDeps}
              onChange={(e) => setInstallInstallDeps(e.target.checked)}
              disabled={!installCreateVenv || busyAction === 'install'}
            />
            自动 pip install requirements.txt
          </label>
          <span className="skill-pack-form-tip">
            <Info size={11} /> 含大依赖的 pack（如 sentence-transformers）首次安装可能需要几分钟
          </span>
        </div>
      </div>

      {/* ===== 推荐 pack ===== */}
      <div className="skill-section-header">
        <span>精选推荐</span>
      </div>
      <div className="skill-pack-featured">
        {FEATURED_PACKS.map((p) => {
          const installed = Array.from(installedNames).some(
            (name) => name === p.source.split('/')[1],
          )
          return (
            <div key={p.source} className="skill-pack-featured-card">
              <div className="skill-pack-featured-head">
                <div className="skill-pack-featured-title">
                  {p.display_name}
                  <span className="skill-pack-tag">{p.category}</span>
                </div>
                <a
                  className="skill-pack-link"
                  href={`https://github.com/${p.source}`}
                  target="_blank"
                  rel="noreferrer"
                  title="在 GitHub 打开"
                >
                  <ExternalLink size={12} /> {p.source}
                </a>
              </div>
              <p className="skill-pack-featured-tagline">{p.tagline}</p>
              {p.install_size_hint && (
                <p className="skill-pack-featured-meta">体积：{p.install_size_hint}</p>
              )}
              <div className="skill-pack-featured-actions">
                {installed ? (
                  <span className="skill-pack-installed-tag">
                    <Check size={12} /> 已安装
                  </span>
                ) : (
                  <button
                    className="skill-pack-btn-secondary"
                    disabled={busyAction !== null}
                    onClick={() => void handleInstall(p.source)}
                  >
                    {busyPack === p.source && busyAction === 'install' ? (
                      <>
                        <Loader2 size={12} className="spin" /> 安装中…
                      </>
                    ) : (
                      <>
                        <Plus size={12} /> 一键安装
                      </>
                    )}
                  </button>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {/* ===== 已安装列表 ===== */}
      <div className="skill-section-header">
        <span>已安装</span>
        <span className="skill-count">{packs.length} 个</span>
      </div>

      {loading && packs.length === 0 ? (
        <div className="empty-state">
          <RefreshCw size={20} className="spin" />
          <p>加载中…</p>
        </div>
      ) : packs.length === 0 ? (
        <div className="empty-state">
          <Package size={28} />
          <p>还没安装任何插件包。</p>
        </div>
      ) : (
        <ul className="skill-pack-list">
          {packs.map((p) => {
            const isBusyHere = busyPack === p.pack_name
            return (
              <li key={p.pack_name} className="skill-pack-list-item">
                <div className="skill-pack-list-main">
                  <div className="skill-pack-list-title">
                    <Package size={14} />
                    <strong>{p.plugin_name || p.pack_name}</strong>
                    {p.installed_version && (
                      <span className="skill-pack-version">v{p.installed_version}</span>
                    )}
                    {p.has_venv && (
                      <span className="skill-pack-tag skill-pack-tag--ok">
                        <CheckCircle2 size={10} /> venv
                      </span>
                    )}
                    {!p.managed_by_liuruyan && (
                      <span className="skill-pack-tag skill-pack-tag--warn">
                        手动安装
                      </span>
                    )}
                  </div>
                  {p.plugin_description && (
                    <p className="skill-pack-list-desc">{p.plugin_description}</p>
                  )}
                  <div className="skill-pack-list-meta">
                    <span title="来源">
                      <Github size={11} /> {formatSource(p.source)}
                    </span>
                    <span title="安装时间">装于 {formatTime(p.installed_at)}</span>
                    {p.last_updated_at && (
                      <span title="最近升级">升于 {formatTime(p.last_updated_at)}</span>
                    )}
                  </div>
                </div>
                <div className="skill-pack-list-actions">
                  <button
                    className="skill-pack-btn-secondary"
                    disabled={busyAction !== null || !p.managed_by_liuruyan}
                    onClick={() => void handleUpdate(p.pack_name)}
                    title={p.managed_by_liuruyan ? '升级（git pull）' : '手动安装的 pack 不支持升级'}
                  >
                    {isBusyHere && busyAction === 'update' ? (
                      <>
                        <Loader2 size={12} className="spin" /> 升级中…
                      </>
                    ) : (
                      <>
                        <Upload size={12} /> 升级
                      </>
                    )}
                  </button>
                  <button
                    className="skill-pack-btn-danger"
                    disabled={busyAction !== null}
                    onClick={() => void handleUninstall(p.pack_name)}
                    title="卸载"
                  >
                    {isBusyHere && busyAction === 'uninstall' ? (
                      <>
                        <Loader2 size={12} className="spin" />
                        卸载中
                      </>
                    ) : (
                      <>
                        <Trash2 size={12} />
                        卸载
                      </>
                    )}
                  </button>
                </div>
              </li>
            )
          })}
        </ul>
      )}

      {confirmDialog}
    </div>
  )
}
