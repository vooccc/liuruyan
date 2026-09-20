/**
 * 设置 → 远程访问 面板（M8a）。
 *
 * 让用户开启/关闭远程访问（监听 0.0.0.0），并显示前端 dist 挂载状态、Tailscale
 * 使用指南、安全建议。
 *
 * UI 设计原则（M8a 阶段）：
 *   - 先求"能用"，不追求 polish——M8b 会按真实痛点迭代
 *   - 颜色全部走项目全局主题变量（--bg-color / --text-color / ...），
 *     不另起名、不写 hardcoded fallback——主题 JS 切换时自动跟随。
 *   - 整面板 width:100% + minWidth:0 + box-sizing:border-box（settings 容器纪律）
 *
 * 设计文档：_docs/REMOTE_M8A.md
 * 隔离契约：.windsurf/rules/remote-isolation.md
 */
import { useCallback, useEffect, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  RotateCcw,
  Shield,
  Smartphone,
} from 'lucide-react'

import {
  getRemoteStatus,
  updateRemoteConfig,
  type RemoteStatus,
} from '../../api/remote'

type ConfirmMode = 'enable' | 'disable' | null

export default function RemoteAccessPanel() {
  const [status, setStatus] = useState<RemoteStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<ConfirmMode>(null)
  const [restartHint, setRestartHint] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const s = await getRemoteStatus()
      setStatus(s)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const handleConfirmEnable = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await updateRemoteConfig({ enabled: true, host: '0.0.0.0' })
      setRestartHint(result.needs_restart)
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
      setConfirming(null)
    }
  }, [load])

  const handleConfirmDisable = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await updateRemoteConfig({ enabled: false, host: '127.0.0.1' })
      setRestartHint(result.needs_restart)
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
      setConfirming(null)
    }
  }, [load])

  if (loading) {
    return (
      <div style={panelWrap}>
        <Loader2 size={18} style={{ animation: 'spin 1s linear infinite' }} /> 加载中…
      </div>
    )
  }

  return (
    <div style={panelWrap}>
      <h2 style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 0 }}>
        <Smartphone size={22} /> 远程访问
      </h2>
      <p style={subTitle}>
        让外部设备（手机 / 出差笔记本）通过 Tailscale 等私网访问完整柳如烟 UI。
      </p>

      {/* ============ 状态 Hero ============ */}
      <section style={heroBox}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap' }}>
          <div style={{ minWidth: 0 }}>
            <div style={mutedLabel}>当前状态</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6, fontSize: 16, fontWeight: 600 }}>
              {status?.enabled ? (
                <>
                  <CheckCircle2 size={18} color="var(--accent-green)" />
                  已启用 · 监听 <code style={codeInline}>{status.host}</code>
                </>
              ) : (
                <>
                  <AlertCircle size={18} color="var(--text-muted)" />
                  未启用 · 监听 <code style={codeInline}>{status?.host ?? '127.0.0.1'}</code>
                </>
              )}
            </div>
          </div>
          <button
            type="button"
            onClick={() => setConfirming(status?.enabled ? 'disable' : 'enable')}
            disabled={busy}
            style={status?.enabled ? btnDangerSecondary : btnPrimary}
          >
            {status?.enabled ? '关闭远程访问' : '启用远程访问'}
          </button>
        </div>

        <div style={{ marginTop: 14, fontSize: 13, color: 'var(--text-muted)' }}>
          前端 dist 挂载：{' '}
          {status?.dist_mounted ? (
            <span style={{ color: 'var(--accent-green)' }}>
              ✓ 已挂载{status.dist_path ? `（${status.dist_path}）` : ''}
            </span>
          ) : (
            <span style={{ color: 'var(--accent-red)' }}>
              ✗ 未挂载（
              {status?.mount_skip_reason === 'dist_not_found'
                ? '未找到 frontend/dist，请先 npm run build'
                : status?.mount_skip_reason ?? '未知原因'}
              ）
            </span>
          )}
        </div>
      </section>

      {error && (
        <div style={errorBox}>
          <AlertCircle size={16} style={{ flexShrink: 0, marginTop: 2 }} /> {error}
        </div>
      )}

      {restartHint && (
        <div style={hintBox}>
          <RotateCcw size={18} style={{ flexShrink: 0, marginTop: 2 }} />
          <div>
            <div style={{ fontWeight: 600 }}>需重启柳如烟生效</div>
            <div style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 4 }}>
              配置已保存。请关闭柳如烟主进程再启动，新的监听 host 才会生效。
            </div>
          </div>
        </div>
      )}

      {/* ============ 使用指南 ============ */}
      <section style={{ marginTop: 24 }}>
        <h3 style={sectionTitle}>📖 使用指南</h3>
        <ol style={{ lineHeight: 1.8, paddingLeft: 22, margin: '8px 0' }}>
          <li>
            装 Tailscale：
            <a href="https://tailscale.com/download" target="_blank" rel="noopener noreferrer" style={linkStyle}>
              {' '}tailscale.com/download
            </a>
            ——电脑 + 手机都装，登同一账号
          </li>
          <li>
            <strong>(强烈推荐)</strong> 去 Gateway → 令牌管理，把要给手机用的 token 设
            <code style={codeInline}>allow_ips: ["100.0.0.0/8"]</code>
            （Tailscale 段）
          </li>
          <li>点击上方"启用远程访问"按钮 → 弹确认对话框 → 同意</li>
          <li>
            <strong>重启柳如烟</strong>（关闭主进程后再启动；M8a 不支持热切换）
          </li>
          <li>
            手机浏览器访问 <code style={codeInline}>http://&lt;你电脑名&gt;.&lt;tailnet&gt;.ts.net:8001</code>
            （电脑名在 Tailscale 客户端能看到）
          </li>
          <li>Safari / Chrome → 分享 / 菜单 → 添加到主屏幕 → 桌面出现柳如烟 PWA 图标</li>
        </ol>
      </section>

      {/* ============ 安全模型 ============ */}
      <section style={{ marginTop: 24 }}>
        <h3 style={sectionTitle}>
          <Shield size={16} style={{ verticalAlign: 'text-bottom', marginRight: 4 }} />
          安全模型（三道锁）
        </h3>
        <ul style={{ lineHeight: 1.8, paddingLeft: 22, fontSize: 14, margin: '8px 0' }}>
          <li>
            <strong>Tailscale 网络层</strong>：WireGuard 端到端加密 + 账号验证（外人进不来私网）
          </li>
          <li>
            <strong>Gateway token + IP 白名单</strong>：每个 token 限定调用方 IP 段，泄漏到公网也调不动
          </li>
          <li>
            <strong>admin 端点 loopback-only</strong>：远程访问开关只能从本机改，远程客户端访问
            <code style={codeInline}>/remote/admin/*</code> 一律 403
          </li>
        </ul>
        <p style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 12 }}>
          完整设计：<code style={codeInline}>_docs/REMOTE_M8A.md</code>
        </p>
      </section>

      {/* ============ 确认对话框 ============ */}
      {confirming === 'enable' && (
        <ConfirmDialog
          title="启用远程访问？"
          description={
            <>
              <p>
                启用后柳如烟会监听 <code style={codeInline}>0.0.0.0:8001</code>，
                同一局域网 / Tailscale 网内的设备都能访问。
              </p>
              <p style={{ color: 'var(--accent-red)' }}>
                ⚠️ <strong>强烈建议先去 Gateway 设 IP 白名单</strong>（
                <code style={codeInline}>100.0.0.0/8</code>），避免 token 泄漏到公网时被滥用。
              </p>
              <p style={{ fontSize: 13, color: 'var(--text-muted)' }}>
                设置保存后需手动重启柳如烟才生效。
              </p>
            </>
          }
          onConfirm={handleConfirmEnable}
          onCancel={() => setConfirming(null)}
          busy={busy}
          confirmLabel="启用"
        />
      )}
      {confirming === 'disable' && (
        <ConfirmDialog
          title="关闭远程访问？"
          description={
            <p>
              关闭后柳如烟会回到只监听 <code style={codeInline}>127.0.0.1</code>，
              外部设备将无法访问。重启后生效。
            </p>
          }
          onConfirm={handleConfirmDisable}
          onCancel={() => setConfirming(null)}
          busy={busy}
          confirmLabel="关闭"
        />
      )}
    </div>
  )
}

// ──────────────────────── ConfirmDialog ────────────────────────

interface ConfirmDialogProps {
  title: string
  description: React.ReactNode
  onConfirm: () => void
  onCancel: () => void
  busy: boolean
  confirmLabel: string
}

function ConfirmDialog({
  title,
  description,
  onConfirm,
  onCancel,
  busy,
  confirmLabel,
}: ConfirmDialogProps) {
  return (
    <div
      onClick={onCancel}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.55)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 9999,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--bg-secondary)',
          color: 'var(--text-color)',
          borderRadius: 8,
          padding: '24px 28px',
          maxWidth: 480,
          width: '90%',
          border: '1px solid var(--border-color)',
          boxShadow: '0 12px 40px var(--shadow-md)',
        }}
      >
        <h3 style={{ marginTop: 0, marginBottom: 12 }}>{title}</h3>
        <div style={{ fontSize: 14, lineHeight: 1.6 }}>{description}</div>
        <div
          style={{
            display: 'flex',
            gap: 10,
            justifyContent: 'flex-end',
            marginTop: 24,
          }}
        >
          <button type="button" onClick={onCancel} disabled={busy} style={btnSecondary}>
            取消
          </button>
          <button type="button" onClick={onConfirm} disabled={busy} style={btnPrimary}>
            {busy ? '处理中…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}

// ──────────────────────── 样式片段（inline，M8a polish 留给 M8b） ────────────────────────

const panelWrap: React.CSSProperties = {
  width: '100%',
  minWidth: 0,
  boxSizing: 'border-box',
  padding: '24px 28px',
  overflowX: 'auto',
}

const subTitle: React.CSSProperties = {
  color: 'var(--text-muted)',
  margin: '4px 0 20px 0',
  fontSize: 14,
}

const heroBox: React.CSSProperties = {
  background: 'var(--bg-card)',
  border: '1px solid var(--border-color)',
  borderRadius: 8,
  padding: '16px 20px',
}

const mutedLabel: React.CSSProperties = {
  fontSize: 12,
  color: 'var(--text-muted)',
  textTransform: 'uppercase',
  letterSpacing: 0.5,
}

const codeInline: React.CSSProperties = {
  background: 'var(--bg-tertiary)',
  color: 'var(--text-color)',
  padding: '2px 6px',
  borderRadius: 4,
  fontSize: '0.9em',
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
}

const sectionTitle: React.CSSProperties = {
  fontSize: 15,
  fontWeight: 600,
  margin: '12px 0 8px 0',
  color: 'var(--text-color)',
}

const linkStyle: React.CSSProperties = {
  color: 'var(--accent-blue)',
  textDecoration: 'none',
}

// 错误 / 提示 banner 两个用半透明背景 + 主题 accent 文字：
// 类似 rgba(248,81,73,0.1) 的背景在白底黑底都表现 OK，文字用主题 accent
// 则能跟随主题颁发。
const errorBox: React.CSSProperties = {
  background: 'rgba(248,81,73,0.1)',
  border: '1px solid rgba(248,81,73,0.3)',
  borderRadius: 6,
  padding: '10px 14px',
  marginTop: 14,
  color: 'var(--accent-red)',
  fontSize: 14,
  display: 'flex',
  gap: 8,
  alignItems: 'flex-start',
}

const hintBox: React.CSSProperties = {
  background: 'rgba(255,200,80,0.1)',
  border: '1px solid rgba(255,200,80,0.4)',
  borderRadius: 6,
  padding: '12px 16px',
  marginTop: 14,
  display: 'flex',
  gap: 10,
  alignItems: 'flex-start',
  color: 'var(--text-color)',
}

const btnPrimary: React.CSSProperties = {
  padding: '8px 18px',
  borderRadius: 6,
  border: '1px solid var(--accent-green)',
  background: 'var(--accent-green)',
  color: '#fff',
  fontWeight: 500,
  cursor: 'pointer',
}

const btnSecondary: React.CSSProperties = {
  padding: '8px 18px',
  borderRadius: 6,
  border: '1px solid var(--border-color)',
  background: 'transparent',
  color: 'var(--text-color)',
  cursor: 'pointer',
}

const btnDangerSecondary: React.CSSProperties = {
  padding: '8px 18px',
  borderRadius: 6,
  border: '1px solid var(--accent-red)',
  background: 'transparent',
  color: 'var(--accent-red)',
  cursor: 'pointer',
}
