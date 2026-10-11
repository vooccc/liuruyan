/**
 * 柳如烟 PWA Service Worker（M8a）。
 *
 * 策略：两套读模型，按资源性质分开：
 *
 *   ┌──────────────────┬──────────────────────┬─────────────────────────────────┐
 *   │ 资源类型         │ 读模型               │ 理由                            │
 *   ├──────────────────┼──────────────────────┼─────────────────────────────────┤
 *   │ HTML 导航 / shell│ network-first        │ index.html 引用的 hash chunk 会随│
 *   │ (/、/index.html) │ (失败才用缓存)       │ 部署变化；如果 cache-first 老用户│
 *   │                  │                      │ 永远拿旧版指向旧 chunk，再 build │
 *   │                  │                      │ 也没用——黑屏不可恢复。         │
 *   ├──────────────────┼──────────────────────┼─────────────────────────────────┤
 *   │ vite hash 资源   │ cache-first          │ /assets/xxx-<hash>.js 文件名带  │
 *   │ (含字体 / icon)  │ (命中就用)           │ 内容 hash → immutable，缓存命中 │
 *   │                  │                      │ 永远是对的，省下大量重复请求。  │
 *   ├──────────────────┼──────────────────────┼─────────────────────────────────┤
 *   │ API / SSE / 跨域 │ 不接管               │ 数据要永远最新；流式请求被缓存  │
 *   │                  │                      │ 会卡死；跨域 CORS 复杂别趟。    │
 *   └──────────────────┴──────────────────────┴─────────────────────────────────┘
 *
 * 离线兜底：所有 navigation 网络失败时回退到 caches.match('/index.html')——保证
 * 用户离线/弱网仍能打开 PWA 主界面（虽然 API 调不通，但 React app 能 render）。
 * 设计文档：docs/REMOTE_SPEC.md
 * 隔离契约：远程终端安全隔离规范
 * 版本号约定：改 SW 行为时把 CACHE 后缀 +1（v1 → v2 → ...），activate 阶段
 * 会清掉所有非当前版本的缓存——这是修复"老用户卡在旧 SW"的关键开关。
 *
 * 历史教训（v1 → v2）：v1 的 fetch handler 对 HTML 也用 cache-first，导致
 *   重新部署后 SW 永远返旧 index.html → 浏览器永远请求旧 hash chunk
 *   → 旧 JS 里 hardcoded API_BASE='127.0.0.1' 在 M8a 远程访问场景下 fetch 失败
 *   → 用户手机访问表现为"启动黑屏，怎么刷新都不变"。v2 把 HTML 切 network-first
 *   后老用户刷新两次即可拿到新版（第一次后台 install 新 SW，第二次新 SW 接管）。
 */

const CACHE = 'liuruyan-shell-v2'
const SHELL = ['/', '/index.html', '/manifest.json', '/favicon.png', '/favicon.svg']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      // 每个 URL 单独 try——某个 shell 资源 404 不要阻塞整体 install
      Promise.all(SHELL.map((url) => cache.add(url).catch(() => undefined)))
    )
  )
  // 新 SW 安装完立刻进入 active（不等当前 SW 自然失活）
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE)
          .map((k) => caches.delete(k))
      )
    )
  )
  // 立刻接管所有打开的页面（不等下一次导航）
  self.clients.claim()
})

self.addEventListener('fetch', (event) => {
  const req = event.request

  // 非 GET 一律不接管（POST / PUT 等 mutation 永远走 network）
  if (req.method !== 'GET') return

  let url
  try {
    url = new URL(req.url)
  } catch {
    return
  }

  // 跨域请求不接管
  if (url.origin !== self.location.origin) return

  // SSE 流式请求永不接管
  if (req.headers.get('accept') === 'text/event-stream') return

  // 资源分类
  const isShellHtml = SHELL.includes(url.pathname)
  const isNavigation = req.mode === 'navigate'
  const isAsset = /\.(?:js|mjs|css|woff2?|ttf|otf|eot|png|svg|webp|ico|jpg|jpeg|gif|map|json)$/i.test(
    url.pathname
  )

  // 三类之外一律不接管（API / 其他动态请求让浏览器走默认 fetch）
  if (!(isShellHtml || isNavigation || isAsset)) return

  // ─────────────── HTML / 导航请求：network-first ───────────────
  // 每次先尝试 fetch 最新版（拿到新 index.html → 引用新 hash chunk）。
  // 网络失败才 fall back 到缓存——保证离线/弱网仍能进入 PWA 主界面。
  if (isNavigation || isShellHtml) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok && res.type === 'basic') {
            const clone = res.clone()
            caches.open(CACHE).then((c) => c.put(req, clone).catch(() => undefined))
          }
          return res
        })
        .catch(() =>
          // 离线兜底：先看请求本身的缓存，没有就退到 index.html（SPA 起点）
          caches.match(req).then((c) => c || caches.match('/index.html'))
        )
    )
    return
  }

  // ─────────────── 静态资源：cache-first ───────────────
  // vite 文件名带内容 hash 是 immutable，缓存命中永远是对的；新版本走新 hash 文件名
  // → 自然走 network 拉一次再 cache，零冲突。
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached
      return fetch(req)
        .then((res) => {
          if (res.ok && res.type === 'basic') {
            const clone = res.clone()
            caches.open(CACHE).then((c) => c.put(req, clone).catch(() => undefined))
          }
          return res
        })
        .catch(() => Response.error())
    })
  )
})
