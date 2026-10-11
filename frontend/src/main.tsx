import React, { lazy, Suspense, useEffect, useState } from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import CharacterWorkshop, { type WorkshopMode } from './components/CharacterWorkshop'
import MemoryPanel from './components/MemoryPanel'
// 字体：全部走可变字体（variable），一个文件覆盖 100-900 全字重。
// 换掉原先思源黑体的 300/400/500/700 四档静态字重 —— 那是 408 个 woff2 分片、
// 13.6MB 全打进 Tauri 安装包，且没有 600 档，导致标题 font-weight:600 时
// 中文被吸附到 700、比英文粗一档。可变字体同时解决体积和字重对齐两个问题。
import '@fontsource-variable/inter'
import '@fontsource-variable/noto-sans-sc'
import '@fontsource-variable/jetbrains-mono'
import './index.css'
import './styles/typewriter.css'
import { initDevTelemetry } from './lib/devTelemetry'
import { installExternalLinkInterceptor } from './lib/externalLinks'
import { registerPWA } from './remote/pwa'
import { applyTheme, getStoredTheme } from './theme'
import { Agent, ensureDefaultAgent } from './api/chat'

// 路径 A：开机即起前端遥测，让 AI 用 read_frontend_events 看 console/网络/异常。
// 关闭：localStorage.setItem('LIURUYAN_DISABLE_TELEMETRY', '1') + 刷新。
// 设计文档：./frontend\src\lib\devTelemetry.ts
initDevTelemetry()

// 2026-05-18：拦截所有外部链接 click，避免 Tauri/VSCode webview 单 frame 直接导航
// 把柳如烟整个界面替换掉。chatMarkdown 的 target=_blank 在某些 webview 里被吞 → 没反应，
// 这里统一改走 window.open / Tauri shell.open。
installExternalLinkInterceptor()

// M8a：PWA Service Worker 注册。
// 仅在普通浏览器（runtime.kind === 'web'）注册；Tauri 桌面壳 / VSCode webview 自动跳过。
// 让用户在手机 Safari/Chrome 把柳如烟"添加到主屏"后接近原生 app 体验。
// 设计文档：docs/REMOTE_SPEC.md ；隔离契约：远程安全隔离规范
void registerPWA()

// [DEV ONLY] hash-based routing for new EventBus demo page.
// Visit http://localhost:1420/#dev-eventbus to render this instead of the main App.
// This is purely additive — main App is untouched.
const EventBusDemo = lazy(() => import('./components/_dev/EventBusDemo'))

function StandaloneMemoryBrowser({ agentId, onClose }: { agentId?: string; onClose: () => void }) {
  const [agents, setAgents] = useState<Agent[]>([])

  useEffect(() => {
    let cancelled = false
    ensureDefaultAgent()
      .then((nextAgents) => {
        if (!cancelled) setAgents(nextAgents)
      })
      .catch(() => {
        if (!cancelled) setAgents([])
      })
    return () => { cancelled = true }
  }, [])

  return <MemoryPanel onClose={onClose} agentId={agentId} agents={agents} standalone />
}

function Root() {
  const hash = typeof window !== 'undefined' ? window.location.hash : ''
  const isDevDemo = hash.startsWith('#dev-eventbus')
  const isWorkshopWindow = hash.startsWith('#character-workshop')
  const isMemoryWindow = hash.startsWith('#memory-browser')

  async function closeStandaloneWindow() {
    try {
      const { getCurrentWindow } = await import('@tauri-apps/api/window')
      await getCurrentWindow().close()
    } catch {
      window.close()
    }
  }

  if (isWorkshopWindow) {
    applyTheme(getStoredTheme())
    const params = new URLSearchParams(hash.split('?')[1] || '')
    const mode = (params.get('mode') === 'create' ? 'create' : 'edit') as WorkshopMode
    const agentId = params.get('agentId') || undefined
    return (
      <CharacterWorkshop
        open
        standalone
        mode={mode}
        agentId={agentId}
        onClose={() => { void closeStandaloneWindow() }}
        onAgentCreated={async () => {
          const { emitTo } = await import('@tauri-apps/api/event')
          await emitTo('main', 'liuruyan://agents-changed')
        }}
        onAgentUpdated={async () => {
          const { emitTo } = await import('@tauri-apps/api/event')
          await emitTo('main', 'liuruyan://agents-changed')
        }}
      />
    )
  }

  if (isMemoryWindow) {
    applyTheme(getStoredTheme())
    const params = new URLSearchParams(hash.split('?')[1] || '')
    const agentId = params.get('agentId') || undefined
    return <StandaloneMemoryBrowser agentId={agentId} onClose={() => { void closeStandaloneWindow() }} />
  }

  if (isDevDemo) {
    return (
      <Suspense fallback={<div style={{ padding: 24 }}>Loading dev demo...</div>}>
        <EventBusDemo />
      </Suspense>
    )
  }
  return <App />
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
)
