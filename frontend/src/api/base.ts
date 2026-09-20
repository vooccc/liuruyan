/**
 * 后端 API base URL 单点入口。
 *
 * 三种部署形态各有不同的"前端 origin ↔ 后端位置"关系：
 *
 *   ┌───────────────────┬──────────────────┬─────────────────┬──────────┐
 *   │ 场景              │ 前端从哪加载     │ 后端 API 在哪   │ 是否同源 │
 *   ├───────────────────┼──────────────────┼─────────────────┼──────────┤
 *   │ vite dev          │ localhost:1420   │ 127.0.0.1:8002  │ 跨 origin│
 *   │ Tauri 桌面 app    │ tauri.localhost  │ 127.0.0.1:8001  │ 跨 origin│
 *   │ M8a 远程访问      │ <host>:8001      │ 同进程同端口    │ 同 origin│
 *   │ 未来反向代理      │ app.example.com  │ 反代后面同进程  │ 同 origin│
 *   └───────────────────┴──────────────────┴─────────────────┴──────────┘
 *
 * 判定：当前页面是否就是后端 serve 出来的（同 origin）？
 *   是 → 用 window.location.origin（M8a / 反代场景全覆盖）
 *   否 → VITE_API_BASE 优先；未设置时 fallback 到 127.0.0.1:8001（Tauri sidecar）
 *
 * 信号选择：用 hostname 黑名单（localhost / 127.0.0.1 / 0.0.0.0 / tauri.localhost）反向
 * 推断"是否跨 origin 场景"。比依赖 runtime kind 探测（async）更省事——base.ts 必须同步求值
 * （所有 api/* 模块在 import 时直接读 API_BASE）。
 *
 * 可被 VITE_API_BASE 环境变量强制覆盖（CI / 特殊部署用）。
 *
 * 以后想改成动态端口：
 *   1. Rust sidecar 用 port=0 + parse stdout 拿真实端口、暴露 get_api_base command
 *   2. 这里导出 async getApiBase()；所有调用方 await 它
 * 现阶段单机单实例 + 远程同源场景，固定端口已经够用。
 */

function computeApiBase(): string {
  // 1) 显式环境变量最高优先（CI / 用户自定义部署）
  const envBase = import.meta.env.VITE_API_BASE as string | undefined
  if (envBase) return envBase

  // 2) 同源后端判定
  //    需要 window 存在（SSR 安全）+ protocol 是 http(s)（排除 tauri: / vscode-webview: / file:）
  if (
    typeof window !== 'undefined' &&
    (window.location.protocol === 'http:' || window.location.protocol === 'https:')
  ) {
    const host = window.location.hostname.toLowerCase()
    // hostname 是本地名 → 跨 origin（vite dev / Tauri webview），走 fallback
    // hostname 不是本地名 → 同 origin（M8a 远程 / 反代），用 location.origin
    const isLocalHostname =
      host === 'localhost' ||
      host === '127.0.0.1' ||
      host === '0.0.0.0' ||
      host === 'tauri.localhost'
    if (!isLocalHostname) {
      return window.location.origin
    }
  }

  // 3) 默认：Tauri sidecar / VSCode webview 打 127.0.0.1:8001。
  //    vite dev 由启动.bat 显式注入 VITE_API_BASE=http://127.0.0.1:8002。
  return 'http://127.0.0.1:8001'
}

export const API_BASE: string = computeApiBase()

/** 提取 hostname:port 前缀（不含 http scheme），用于 UI 文案展示 */
export const API_HOST_PORT = API_BASE.replace(/^https?:\/\//, '')

/**
 * 等后端 /health 就绪再返回。轮询 200ms 一次。
 *
 * 背景：Tauri 打包后 sidecar 要解压 + 启 uvicorn，大约 1~3s 才能接请求。
 * 前端 mount 瞬间直接 fetch 会 404/失败；更糟的是 <img> 的 404 会被浏览器负缓存，
 * 即使后端之后 ready 了也不会重试 → 头像留在 emoji，要 F5 才恢复。
 *
 * 结果 Promise 缓存，多处调用共享一次轮询。
 *
 * 注意：不要把这里改成"监听 Tauri backend-ready 事件 + poll race"——看起来能省 200ms
 * 的 poll 间隔，实际代价是 dev 下多一次 vite "new dependencies optimized, reloading"
 * 全栏重启（几秒），毫不划算。保持纯 poll，dev/prod 一套代码。
 */
let _backendReady: Promise<boolean> | null = null

export function waitForBackend(timeoutMs = 45000): Promise<boolean> {
  if (_backendReady) return _backendReady
  _backendReady = (async () => {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      try {
        const res = await fetch(`${API_BASE}/health`)
        if (res.ok) return true
      } catch {
        /* 后端还没起，继续轮询 */
      }
      await new Promise((r) => setTimeout(r, 200))
    }
    _backendReady = null
    return false
  })()
  return _backendReady
}
