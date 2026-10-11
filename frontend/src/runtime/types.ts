/**
 * Runtime 抽象层 —— 把 IDE / 桌面端 / 浏览器的差异收口到一个接口。
 *
 * 背景：柳如烟 frontend 既要跑在 Tauri 桌面端（用 @tauri-apps/api），
 * 也要跑在 VSCode Webview（@tauri-apps/api 不可用），将来还可能跑在纯浏览器。
 *
 * 设计原则（见 docs/ide_integration_principles.md）：
 *   - 前端业务代码统一调 runtime.window.xxx() / runtime.fs.xxx()
 *   - 各运行时实现各自包装环境特定 API（Tauri / VSCode HTTP / 浏览器原生）
 *   - 添加新运行时只需新增一个 implementation 文件，业务代码零修改
 *
 * 阶段 1 接口范围：仅覆盖 App.tsx + exportImport.ts 现有的 Tauri API 调用，
 * 不预设未来需求。后续按 §12 TODO 推进时再扩展。
 */

export type RuntimeKind = 'tauri' | 'vscode' | 'web'

/** 窗口控制：minimize / maximize / close / drag / resize 等 chrome 操作 */
export interface RuntimeWindow {
  minimize(): Promise<void>
  toggleMaximize(): Promise<void>
  isMaximized(): Promise<boolean>
  close(): Promise<void>
  startDragging(): Promise<void>
  /** 注册 resize 监听，返回 unlisten 函数 */
  onResized(cb: () => void): Promise<() => void>
}

/** native file picker */
export interface RuntimeDialog {
  /** Save As 对话框，返回用户选择的目标路径或 null（取消） */
  save(options: {
    defaultPath?: string
    filters?: { name: string; extensions: string[] }[]
  }): Promise<string | null>

  /**
   * 选择目录（用于 claude code / codex 一键启动时挑工作区根目录等）。
   * 返回用户选择的绝对路径，或 null（用户取消）。
   *
   * runtime 实现：
   *   - Tauri：@tauri-apps/plugin-dialog open({ directory: true })
   *   - VSCode webview / web：暂未实现 → 抛错；调用方应 catch 并 fallback 到手动输入
   */
  pickDirectory(options?: {
    defaultPath?: string
    title?: string
  }): Promise<string | null>
}

/** 文件系统：阶段 1 只暴露 exportImport 用到的最小集，后续按需扩展 */
export interface RuntimeFs {
  /** 复制文件，源/目标都是绝对路径 */
  copyFile(source: string, target: string): Promise<void>
}

/**
 * Shell：调用系统能力。
 *
 * 对齐 opencode `Platform.openLink` / Cherry `shell.openExternal` 的设计：
 * 把 "用系统默认应用打开 URL" 这件事抽到 runtime 层，避免业务代码到处嗅探环境。
 *
 * 各 runtime 实现：
 *   - Tauri：invoke('open_external') → Rust 端 shell.open
 *   - VSCode / web：window.open(url, '_blank')（VSCode webview 会弹确认框）
 */
export interface RuntimeShell {
  /** 用系统默认应用打开 URL（http/https/mailto/tel）。失败仅 console.warn，不抛。 */
  openExternal(url: string): Promise<void>
}

/** 诊断项（跟插件端 normalizeDiagnostic 输出保持一致） */
export interface DiagnosticItem {
  uri: string
  fsPath: string
  severity: 'error' | 'warning' | 'info' | 'hint'
  message: string
  source: string | null
  code: string | null
  range: {
    start: { line: number; character: number }
    end: { line: number; character: number }
  }
}

/** IDE 上下文：仅 vscode runtime 提供完整实现，其他运行时为 null */
export interface RuntimeIde {
  getActiveEditor(): Promise<{
    uri: string
    content: string
    selection?: { start: number; end: number; text: string }
  } | null>
  getSelection(): Promise<{ uri: string; text: string } | null>
  /** 在目标编辑器插入/替换文本；line/character 用 0-indexed */
  insertSnippet(opts: {
    text: string
    uri?: string
    line?: number
    character?: number
    replace?: boolean
  }): Promise<{ applied: boolean; uri: string; lineCount: number }>
  /** 让 IDE 打开指定文件，line/character 用 0-indexed */
  openFile(opts: {
    uri?: string
    fsPath?: string
    line?: number
    character?: number
    preview?: boolean
    viewColumn?: 'active' | 'beside'
  }): Promise<{ opened: boolean; uri: string; lineCount: number }>
  /** 拉诊断信息；scope='current'（当前编辑器，默认）| 'all'（全 workspace）| uri */
  getDiagnostics(opts?: { scope?: 'current' | 'all'; uri?: string }): Promise<DiagnosticItem[]>
  /**
   * 让用户在 IDE 里 review 整文件修改 + 决定是否落盘。
   * full-file 模式：newContent 是完整目标文件内容（不是 patch）。
   *
   * v2 inline diff（默认）：buffer 立刻显示 newContent + 每个 hunk 上方 Accept/Reject CodeLens
   * v1 modal（fallback，env LIURUYAN_INLINE_DIFF=0）：vscode.diff view + 整文件模态 Apply/Reject
   *
   * 返回 applied 表示用户是否至少 accept 了一个 hunk；reason 给 LLM 看的描述。
   * v2 路径额外返回 sessionId/accepted/rejected/total/status（v1 路径不返回，optional）。
   */
  applyEdit(opts: {
    uri?: string
    fsPath?: string
    newContent: string
    description?: string
  }): Promise<{
    applied: boolean
    uri: string
    reason: string
    sessionId?: string
    accepted?: number
    rejected?: number
    total?: number
    status?: 'completed' | 'aborted' | 'no-op'
  }>
  /** 创建或复用一个 VSCode terminal。reuse=true 时同名 terminal 会被复用。 */
  openTerminal(opts: {
    name?: string
    cwd?: string
    reuse?: boolean
  }): Promise<{ terminal_id: string; name: string; created: boolean }>
  /**
   * 在指定 terminal 跑命令。
   * 默认 wait_for_output=true，会尝试用 VSCode shell integration 抓 output；
   * 抓不到（shell 不支持 / VSCode 旧）就 fire-and-forget 返回 captured=false。
   */
  runCommandInTerminal(opts: {
    terminal_id?: string
    command: string
    wait_for_output?: boolean
    timeout?: number
  }): Promise<{
    sent: true
    output: string | null
    captured: boolean
    exitCode?: number | null
    reason?: string
    terminal_id: string
  }>
}

export interface Runtime {
  kind: RuntimeKind
  window: RuntimeWindow
  dialog: RuntimeDialog
  fs: RuntimeFs
  shell: RuntimeShell
  /** 仅 VSCode runtime 提供；其他运行时为 null */
  ide: RuntimeIde | null
}
