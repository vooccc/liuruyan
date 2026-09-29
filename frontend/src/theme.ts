export type Theme = 'dark' | 'light'

export const THEMES: Record<Theme, Record<string, string>> = {
  dark: {
    '--primary-color': '#c4a77d',
    '--primary-hover': '#b89a6e',
    '--bg-color': '#0d1117',
    '--bg-secondary': '#161b22',
    '--bg-tertiary': '#21262d',
    '--bg-card': '#1c2128',
    '--text-color': '#e6edf3',
    '--text-secondary': '#8b949e',
    '--text-muted': '#6e7681',
    '--border-color': '#30363d',
    '--border-light': '#21262d',
    '--user-bubble': '#3a342b',
    '--user-bubble-text': '#e8dcc6',
    '--assistant-bubble': '#1a1d22',
    '--assistant-bubble-text': '#e6edf3',
    '--assistant-border': 'transparent',
    '--assistant-border-color': '#1a1d22',
    '--code-bg': '#161b22',
    '--code-border': 'rgba(255,255,255,0.06)',
    '--code-scrollbar': 'rgba(255,255,255,0.12)',
    '--accent-green': '#3fb950',
    '--accent-red': '#f85149',
    '--accent-blue': '#58a6ff',
    '--accent-purple': '#bc8cff',
    '--shadow-sm': 'rgba(0,0,0,0.3)',
    '--shadow-md': 'rgba(0,0,0,0.5)',
    '--sidebar-bg': '#13181f',
    '--input-bg': '#13181f',
    '--scrollbar-track': '#161b22',
    '--scrollbar-thumb': '#30363d',
  },
  light: {
    '--primary-color': '#8f7040',
    '--primary-hover': '#7d6036',
    '--bg-color': '#faf9f6',
    '--bg-secondary': '#f3f2ef',
    '--bg-tertiary': '#eae8e4',
    '--bg-card': '#ffffff',
    '--text-color': '#1f1c17',
    '--text-secondary': '#5c554a',
    '--text-muted': '#867e73',
    '--border-color': '#d8d5d0',
    '--border-light': '#e9e7e3',
    '--user-bubble': '#f2ece0',
    '--user-bubble-text': '#332617',
    '--assistant-bubble': '#ffffff',
    '--assistant-bubble-text': '#1f1c17',
    '--assistant-border': 'transparent',
    '--assistant-border-color': 'transparent',
    '--code-bg': '#f7f6f3',
    '--code-border': '#eae8e4',
    '--code-scrollbar': 'rgba(0,0,0,0.20)',
    '--accent-green': '#1f7a35',
    '--accent-red': '#b8342b',
    '--accent-blue': '#2b5fa8',
    '--accent-purple': '#7a4fb0',
    '--shadow-sm': 'rgba(0,0,0,0.06)',
    '--shadow-md': 'rgba(0,0,0,0.10)',
    '--sidebar-bg': '#f4f3f0',
    '--input-bg': '#ffffff',
    '--scrollbar-track': '#f0efec',
    '--scrollbar-thumb': '#cfccc7',
  },
}

export function getStoredTheme(): Theme {
  const stored = localStorage.getItem('theme')
  return stored === 'dark' ? 'dark' : 'light'
}

/**
 * 两个主题键的并集。
 *
 * applyTheme 写的是 documentElement 的 inline style，而 inline style 不会因为
 * 切主题自动失效 —— 只要某个变量"A 主题有、B 主题没有"，从 A 切到 B 时 A 的值
 * 就会原样残留，一直盖住 index.css 里 :root 的默认值，直到刷新页面。
 *
 * 2026-07-26 实际踩到的例子：--shadow-md 当时只在 light 里有。
 * 暗色启动 → :root 的 rgba(0,0,0,0.5)，正确；切到亮色 → 0.12，正确；
 * 再切回暗色 → dark 的表里没有这个键，0.12 留在原地，14 处阴影全部按亮色
 * 的浓度渲染。已经把 --shadow-md 补进 dark，但根子在下面这个清理逻辑缺失，
 * 所以一并补上，以后再出现单边键也不会重演。
 */
const ALL_THEME_VARS = Array.from(
  new Set(Object.values(THEMES).flatMap((vars) => Object.keys(vars)))
)

export function applyTheme(theme: Theme) {
  const vars = THEMES[theme]
  const root = document.documentElement

  // 先清掉"上一个主题设过、当前主题没有"的键，让它们退回 :root 的默认值
  ALL_THEME_VARS.forEach((key) => {
    if (!(key in vars)) root.style.removeProperty(key)
  })

  Object.entries(vars).forEach(([key, value]) => {
    root.style.setProperty(key, value)
  })
  root.setAttribute('data-theme', theme)
  localStorage.setItem('theme', theme)
}
