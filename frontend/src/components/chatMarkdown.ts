import { marked } from 'marked'
// 语法高亮（2026-07-26）：只引 core + 按需注册语言，全量 highlight.js 是
// 190 种语言 300KB+ gzip，按需这套合计 ~40KB。不用 marked-highlight 接线包：
// 它对 marked 主版本有 peer 范围风险，而 renderer 覆盖自己写只要十行。
import hljs from 'highlight.js/lib/core'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import python from 'highlight.js/lib/languages/python'
import bash from 'highlight.js/lib/languages/bash'
import powershell from 'highlight.js/lib/languages/powershell'
import json from 'highlight.js/lib/languages/json'
import yaml from 'highlight.js/lib/languages/yaml'
import ini from 'highlight.js/lib/languages/ini'
import xml from 'highlight.js/lib/languages/xml'
import css from 'highlight.js/lib/languages/css'
import sql from 'highlight.js/lib/languages/sql'
import rust from 'highlight.js/lib/languages/rust'
import go from 'highlight.js/lib/languages/go'
import markdown from 'highlight.js/lib/languages/markdown'
import diff from 'highlight.js/lib/languages/diff'
import c from 'highlight.js/lib/languages/c'

hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('python', python)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('powershell', powershell)
hljs.registerLanguage('json', json)
hljs.registerLanguage('yaml', yaml)
hljs.registerLanguage('ini', ini)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('css', css)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('rust', rust)
hljs.registerLanguage('go', go)
hljs.registerLanguage('markdown', markdown)
hljs.registerLanguage('diff', diff)
hljs.registerLanguage('c', c)

const LANG_ALIASES: Record<string, string> = {
  js: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript', node: 'javascript',
  ts: 'typescript', tsx: 'typescript',
  py: 'python', py3: 'python',
  sh: 'bash', shell: 'bash', zsh: 'bash', console: 'bash',
  ps: 'powershell', ps1: 'powershell', pwsh: 'powershell',
  yml: 'yaml', toml: 'ini', json5: 'json', jsonc: 'json',
  html: 'xml', svg: 'xml', vue: 'xml',
  rs: 'rust', golang: 'go', md: 'markdown', patch: 'diff', h: 'c',
}

/**
 * 高亮缓存：本项目流式期间每个 chunk 会把整条消息重新 parse 一遍，
 * 不带缓存的话高亮成本 = 代码块数 × chunk 数（长回答会有可感卡顿）。
 * 以 lang+code 全文为 key：已流完的块内容不再变化 → 永远命中；
 * 只有正在增长的最后一个块每 chunk 真算一次。
 * 上限 400 条防泄漏（增长中的块每个 chunk 都会产生一个新 key）。
 */
// 复制按钮图标（lucide copy / check），切换由 CSS 的 .copied 类完成
const COPY_BTN_HTML =
  '<button type="button" class="code-copy-btn" aria-label="复制代码" title="复制">' +
  '<svg class="ic-copy" viewBox="0 0 24 24" width="14" height="14" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>' +
  '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>' +
  '<svg class="ic-check" viewBox="0 0 24 24" width="14" height="14" fill="none" ' +
  'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
  '<polyline points="20 6 9 17 4 12"></polyline></svg></button>'

const HL_CACHE = new Map<string, string>()

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function highlightBlock(code: string, langRaw: string): { html: string; lang: string } {
  const lang = LANG_ALIASES[langRaw] || langRaw
  const key = lang + '\u0001' + code
  const hit = HL_CACHE.get(key)
  if (hit !== undefined) return { html: hit, lang }

  let html: string
  if (lang && hljs.getLanguage(lang)) {
    try {
      html = hljs.highlight(code, { language: lang, ignoreIllegals: true }).value
    } catch {
      html = escapeHtml(code)
    }
  } else {
    // 未标注语言：不做 auto-detect（16 种语言全试一遍太贵，且流式中途
    // 猜测语言会变来变去、颜色闪烁），按纯文本转义输出。
    html = escapeHtml(code)
  }
  if (HL_CACHE.size > 400) HL_CACHE.clear()
  HL_CACHE.set(key, html)
  return { html, lang }
}

marked.setOptions({
  breaks: true,
  gfm: true,
})

marked.use({
  renderer: {
    // marked v13+ 的 renderer.code 收 token 对象（本项目 marked v18）。
    // 输出结构：wrapper（承载边框/底色/圆角）> 头条（语言标签 + 复制按钮）> pre。
    code(token: { text: string; lang?: string }) {
      const raw = token.text ?? ''
      const langRaw = (token.lang || '').trim().split(/\s+/)[0].toLowerCase()
      const { html, lang } = highlightBlock(raw, langRaw)
      // 有语言 → 常规头条（语言左、复制右）；无语言 → 头条转为悬浮在代码
      // 右上角的纯按钮（否则会剩一条空杠）。按钮平时 opacity:0，悬停浮现。
      const headCls = lang ? 'code-block-head' : 'code-block-head no-lang'
      return (
        `<div class="code-block">` +
        `<div class="${headCls}">` +
        `<span class="code-lang">${lang ? escapeHtml(lang) : ''}</span>` +
        COPY_BTN_HTML +
        `</div>` +
        `<pre><code class="hljs">${html}\n</code></pre>` +
        `</div>\n`
      )
    },
  },
})

// ── 复制按钮：事件委托 ────────────────────────────────────────────────────
// 消息 HTML 走 innerHTML 注入，按钮上挂不了 React handler，统一在 document
// 上委托。模块加载时装一次（本模块被聊天组件 import，天然只装一次）。
function fallbackCopy(text: string) {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px'
  document.body.appendChild(ta)
  ta.select()
  try { document.execCommand('copy') } catch { /* 尽力而为 */ }
  ta.remove()
}

let copyDelegationInstalled = false
function installCopyDelegation() {
  if (copyDelegationInstalled || typeof document === 'undefined') return
  copyDelegationInstalled = true
  document.addEventListener('click', (e) => {
    const target = e.target as HTMLElement | null
    const btn = target?.closest?.('.code-copy-btn') as HTMLButtonElement | null
    if (!btn || btn.dataset.busy) return
    const codeEl = btn.closest('.code-block')?.querySelector('pre code')
    const text = codeEl?.textContent ?? ''
    btn.dataset.busy = '1'
    void (async () => {
      try {
        await navigator.clipboard.writeText(text)
      } catch {
        fallbackCopy(text)
      }
      btn.classList.add('copied')
      window.setTimeout(() => {
        btn.classList.remove('copied')
        delete btn.dataset.busy
      }, 1400)
    })()
  })
}
installCopyDelegation()

// 2026-05-18：所有 markdown 链接强制新窗口打开 + 切断 opener 引用。
// 旧默认行为下，点 chat 里出现的 http(s) 链接会让 webview 当前 frame 直接导航过去，
// 把柳如烟整个界面盖掉且无法回退（Tauri / VSCode webview 都会犯这毛病）。
function ensureLinksOpenInNewTab(html: string): string {
  return html.replace(/<a\b([^>]*?)>/gi, (_match, attrs: string) => {
    let next = attrs
    if (/\btarget\s*=/.test(next)) {
      next = next.replace(/\btarget\s*=\s*("[^"]*"|'[^']*'|\S+)/i, 'target="_blank"')
    } else {
      next += ' target="_blank"'
    }
    if (/\brel\s*=/.test(next)) {
      next = next.replace(/\brel\s*=\s*"([^"]*)"/i, (_m, v: string) => {
        const parts = new Set(String(v).split(/\s+/).filter(Boolean))
        parts.add('noopener')
        parts.add('noreferrer')
        return `rel="${Array.from(parts).join(' ')}"`
      })
    } else {
      next += ' rel="noopener noreferrer"'
    }
    return `<a${next}>`
  })
}

const MD_CACHE = new Map<string, string>()
const MAX_MD_CACHE_SIZE = 500

export function renderChatMarkdown(text: string): string {
  if (!text) return ''

  const cached = MD_CACHE.get(text)
  if (cached !== undefined) {
    MD_CACHE.delete(text)
    MD_CACHE.set(text, cached)
    return cached
  }

  let cleaned = text
  if (cleaned.includes('<think')) {
    cleaned = cleaned
      .replace(/<think[^>]*>[\s\S]*?<\/think>/gi, '')
      .replace(/<think[^>]*>/gi, '')
      .replace(/<\/think[^>]*>/gi, '')
  }

  if (!cleaned.trim()) return ''
  const html = ensureLinksOpenInNewTab(marked.parse(cleaned) as string)

  if (MD_CACHE.size >= MAX_MD_CACHE_SIZE) {
    const oldestKey = MD_CACHE.keys().next().value
    if (oldestKey !== undefined) MD_CACHE.delete(oldestKey)
  }
  MD_CACHE.set(text, html)

  return html
}
