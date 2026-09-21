/**
 * IdeStatusBar —— 极简重写版（2026-05-13 04:51 紧急回滚后）。
 *
 * ⛔ webview 三大禁区（实测会引发 "Could not register service worker: InvalidStateError" 白板）：
 *   1. **React Portal** —— createPortal 注入跨 DOM 节点会跟 webview iframe + sw 模型打架
 *   2. **MutationObserver(document.body, subtree:true)** —— body 级 DOM 观察会卷死 sw
 *   3. **document.addEventListener('mousedown'/'click', ...)** —— 任何挂 document 的全局 listener 都炸
 *
 * ✓ 安全的 API：window.addEventListener (CustomEvent)、fetch、useEffect 内 await 函数调用、
 *   普通 React state/onClick。
 *
 * 后果：用户切走再回 webview 时 chip 一加载就白板，必须卸载扩展 + 重启 windsurf 才恢复。
 * 任何想在这个组件加全局监听的人，先读这段，再读 _docs/handover_vscode_extension_phase2_2026_05_13.md。
 *
 * 现在的方式：**纯 inline 渲染**——由 App.tsx 直接把 <IdeStatusBar /> 放进 .window-controls
 * 内 settings 按钮之前作为正常 React 子节点。
 *
 * 交互：click toggle expanded（不是 hover-replace 反模式）。再点 chip 收起。
 * 主题：颜色全 var(--*)，亮/暗主题自适应。
 */

import { useEffect, useRef, useState } from 'react'
import { getRuntime, type Runtime, type RuntimeIde } from '../runtime'
import { isIdeContextEnabled, setIdeContextEnabled } from '../utils/ideContext'

interface ActiveEditorInfo {
  uri: string
  fileName?: string
  selectionText?: string
}

export function IdeStatusBar() {
  const [runtime, setRuntime] = useState<Runtime | null>(null)
  const [editor, setEditor] = useState<ActiveEditorInfo | null>(null)
  const [status, setStatus] = useState<'idle' | 'loading' | 'ok' | 'error'>('idle')
  const [errorMsg, setErrorMsg] = useState<string>('')
  const [ctxEnabled, setCtxEnabled] = useState<boolean>(() => isIdeContextEnabled())
  const [expanded, setExpanded] = useState(false)
  const wrapperRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    let active = true
    void getRuntime().then((r) => {
      if (active) setRuntime(r)
    })
    return () => {
      active = false
    }
  }, [])

  // VSCode runtime 一就绪就自动拉一次 active editor，让 chip 默认显示文件名而不是 "IDE"。
  // 失败静默不报 UI 错误（用户点刷新会自己重试）。
  // ✓ 验证安全：此 effect 只调 ide.getActiveEditor()（走 fetch），不挂任何 document 全局 listener。
  useEffect(() => {
    if (!runtime || runtime.kind !== 'vscode' || !runtime.ide) return
    let cancelled = false
    void runtime.ide.getActiveEditor().then(
      (result) => {
        if (cancelled) return
        if (!result) {
          setEditor(null)
        } else {
          const fileName = result.uri.split(/[\\/]/).pop() || result.uri
          setEditor({ uri: result.uri, fileName, selectionText: result.selection?.text })
        }
        setStatus('ok')
      },
      () => {
        // 静默
      },
    )
    return () => {
      cancelled = true
    }
  }, [runtime])

  // 同窗口跨组件同步 toggle 状态：ideContext.ts 里 setIdeContextEnabled 会 fire 这个 CustomEvent。
  // 不改 store、不动全局状态管理，保持 IdeStatusBar + ideContext helper 两个文件闭环。
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<{ on: boolean }>).detail
      if (detail && typeof detail.on === 'boolean') setCtxEnabled(detail.on)
    }
    window.addEventListener('liuruyan:ideContextToggle', handler)
    return () => window.removeEventListener('liuruyan:ideContextToggle', handler)
  }, [])

  // 非 VSCode 环境完全不渲染
  if (!runtime || runtime.kind !== 'vscode' || !runtime.ide) return null

  const refreshActiveEditor = async (ide: RuntimeIde) => {
    setStatus('loading')
    setErrorMsg('')
    try {
      const result = await ide.getActiveEditor()
      if (!result) {
        setEditor(null)
      } else {
        // 从 uri 里提取文件名（最后一段）
        const fileName = result.uri.split(/[\\/]/).pop() || result.uri
        setEditor({
          uri: result.uri,
          fileName,
          selectionText: result.selection?.text,
        })
      }
      setStatus('ok')
    } catch (err) {
      setStatus('error')
      setErrorMsg(err instanceof Error ? err.message : String(err))
    }
  }

  const statusColor = {
    idle: '#7d8590',
    loading: '#d29922',
    ok: '#3fb950',
    error: '#f85149',
  }[status]

  // 不挂 document.mousedown 实测会炸 webview。再点 chip 自身收起 panel。
  return (
    <div
      ref={wrapperRef}
      style={{
        // inline 渲染进 .window-controls 内（由 App.tsx 放置位置决定排序）。
        // position: relative 给展开态的绝对定位 panel 当锚点。
        position: 'relative',
        display: 'inline-flex',
        alignItems: 'center',
        marginRight: 6,
        fontFamily: 'system-ui, -apple-system, sans-serif',
      }}
    >
      {/* chip 永远渲染（不被 panel 替换，避免"指针上去就消失"的跳动感）。
          点击 toggle 展开；展开时加 .is-active 高亮（跟 settings 按钮 .active 同语言）。
          颜色全 var(--*)，亮/暗主题都自适应。 */}
      <button
        type="button"
        className={`liuruyan-ide-chip${expanded ? ' is-active' : ''}`}
        title="VSCode bridge · 点击查看 / 切换"
        onClick={() => setExpanded((v) => !v)}
      >
        <span
          className="liuruyan-ide-chip__dot"
          style={{ background: statusColor }}
        />
        <span className="liuruyan-ide-chip__label">
          {editor?.fileName || 'IDE'}
        </span>
      </button>

      {expanded && (
        /* 展开态：绝对定位浮在 chip 下方（脱离 window-controls 流，不撑开按钮组）。
           背景/边框走主题 CSS 变量，亮色暗色都好看。 */
        <div
          className="liuruyan-ide-chip__panel"
          style={{
            // absolute + left: 0：panel 左边缘对齐 chip 左边缘，向右展开。
            // maxWidth: calc(100vw - 16px)：不超视口右边界（sidebar 窄模式自动缩窄）。
            position: 'absolute',
            top: 'calc(100% + 4px)',
            left: 0,
            right: 'auto',
            zIndex: 9999,
            padding: '6px 10px',
            borderRadius: 8,
            fontSize: 11,
            minWidth: 200,
            maxWidth: 'calc(100vw - 16px)',
          }}
        >
          {/* 竖排布局：标题 / 当前编辑器 / toggle / 刷新按钮，每行一件事，窄宽度也不挤 */}

          {/* 标题行：状态点 + VSCode bridge */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: statusColor,
                flexShrink: 0,
              }}
            />
            <span style={{ opacity: 0.7, fontSize: 11 }}>VSCode bridge</span>
          </div>

          {/* 当前编辑器信息 */}
          <div style={{ marginBottom: 8, opacity: 0.85 }}>
            {status === 'idle' && <span style={{ fontSize: 10, opacity: 0.6 }}>等待刷新…</span>}
            {status === 'error' && <span style={{ color: '#f85149', fontSize: 10 }}>错误：{errorMsg}</span>}
            {(status === 'ok' || status === 'loading') && !editor && (
              <span style={{ fontSize: 10, opacity: 0.6 }}>IDE 当前无活动编辑器</span>
            )}
            {(status === 'ok' || status === 'loading') && editor && (
              <>
                <div style={{ fontWeight: 600, fontSize: 11, wordBreak: 'break-all' }}>
                  {editor.fileName}
                </div>
                {editor.selectionText && (
                  <div
                    style={{
                      marginTop: 2,
                      fontSize: 10,
                      opacity: 0.6,
                      maxHeight: 40,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    选中: {editor.selectionText.slice(0, 60)}
                  </div>
                )}
              </>
            )}
          </div>

          {/* 注入 toggle —— 整行可点 */}
          <label
            title="开启后，发送聊天时会自动拼一块 IDE 上下文（当前文件路径+选中起止行+选区代码片段）到消息头部，让模型看到你在看什么。关闭后不拼。"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              cursor: 'pointer',
              userSelect: 'none',
              padding: '4px 0',
              fontSize: 11,
            }}
          >
            <input
              type="checkbox"
              checked={ctxEnabled}
              onChange={(e) => {
                const on = e.target.checked
                setCtxEnabled(on)
                setIdeContextEnabled(on)
              }}
              style={{ margin: 0, cursor: 'pointer' }}
            />
            <span style={{ opacity: 0.85 }}>注入 IDE 上下文</span>
          </label>

          {/* 刷新按钮 —— 块级宽（文字只 "刷新" 两字） */}
          <button
            onClick={() => void refreshActiveEditor(runtime.ide!)}
            disabled={status === 'loading'}
            style={{
              display: 'block',
              width: '100%',
              marginTop: 6,
              background: 'transparent',
              color: '#58a6ff',
              border: '1px solid rgba(88, 166, 255, 0.3)',
              borderRadius: 4,
              padding: '4px 8px',
              fontSize: 11,
              cursor: status === 'loading' ? 'wait' : 'pointer',
              fontFamily: 'inherit',
            }}
          >
            {status === 'loading' ? '加载中…' : '刷新'}
          </button>
        </div>
      )}
    </div>
  )
}
