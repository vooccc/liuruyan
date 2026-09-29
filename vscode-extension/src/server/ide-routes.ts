// IDE 桥接路由 —— 把 VSCode Extension API 暴露成 HTTP 端点。
//
// 设计契约（_docs/ide_integration_principles.md）：
//   铁律 4：每个能力一个显式端点（不用通用 RPC "execute any vscode.xxx"）
//   铁律 5：只做 IDE 原生能力桥接 + 序列化，不掺业务逻辑
//   铁律 6：响应 schema 简单、扁平、纯 JSON，前端不用关心 vscode.Uri 对象
//
// CSRF 中间件已在 server/index.ts 里 app.use('/vscode', csrf) 挂好，本文件只管路由。

import type { Express, Request, Response } from 'express'
import * as vscode from 'vscode'
import { getCurrentTextEditor } from '../editor-state'
import { openOrReuseTerminal, getTerminal, executeWithShellIntegration } from '../terminals'
import { log } from '../utils/logger'
import { handleApplyEditV2 } from './applyEditV2'

// Phase 3 v2 inline diff 路径开关：默认走 v2，env LIURUYAN_INLINE_DIFF=0 切回 v1 modal
function shouldUseInlineDiffV2(): boolean {
  return process.env.LIURUYAN_INLINE_DIFF !== '0'
}

// 把 vscode.DiagnosticSeverity 枚举转成字符串，前端不用记住数字与严重性的对应
function severityName(s: vscode.DiagnosticSeverity): 'error' | 'warning' | 'info' | 'hint' {
  switch (s) {
    case vscode.DiagnosticSeverity.Error:
      return 'error'
    case vscode.DiagnosticSeverity.Warning:
      return 'warning'
    case vscode.DiagnosticSeverity.Information:
      return 'info'
    case vscode.DiagnosticSeverity.Hint:
      return 'hint'
    default:
      return 'info'
  }
}

function normalizeDiagnostic(uri: vscode.Uri, d: vscode.Diagnostic) {
  // d.code 可能是 string / number / { value, target } 三种形态，统一拍成字符串或 null
  let code: string | null = null
  if (typeof d.code === 'string' || typeof d.code === 'number') {
    code = String(d.code)
  } else if (d.code && typeof d.code === 'object' && 'value' in d.code) {
    code = String((d.code as { value: string | number }).value)
  }
  return {
    uri: uri.toString(),
    fsPath: uri.fsPath,
    severity: severityName(d.severity),
    message: d.message,
    source: d.source ?? null,
    code,
    range: {
      start: { line: d.range.start.line, character: d.range.start.character },
      end: { line: d.range.end.line, character: d.range.end.character },
    },
  }
}

export function registerIdeRoutes(app: Express): void {
  // ---- GET /vscode/active-editor ----
  // 拿当前活动编辑器文件信息 + 全文内容 + 选中区域（如有）。
  // 无活动编辑器时返回 { ok: true, editor: null }（不当作错误）。
  app.get('/vscode/active-editor', (_req: Request, res: Response) => {
    const editor = getCurrentTextEditor()
    if (!editor) {
      res.json({ ok: true, editor: null })
      return
    }
    const doc = editor.document
    const sel = editor.selection
    res.json({
      ok: true,
      editor: {
        uri: doc.uri.toString(),
        fsPath: doc.uri.fsPath,
        fileName: doc.fileName,
        languageId: doc.languageId,
        lineCount: doc.lineCount,
        content: doc.getText(),
        selection: sel.isEmpty
          ? null
          : {
              start: doc.offsetAt(sel.start),
              end: doc.offsetAt(sel.end),
              text: doc.getText(sel),
            },
      },
    })
  })

  // ---- GET /vscode/selection ----
  // 单独拿选中文本；没选中（或没编辑器）时 selection: null。
  // 轻量端点，不带全文，方便前端在用户选了代码后快速调一下。
  app.get('/vscode/selection', (_req: Request, res: Response) => {
    const editor = getCurrentTextEditor()
    if (!editor || editor.selection.isEmpty) {
      res.json({ ok: true, selection: null })
      return
    }
    const doc = editor.document
    res.json({
      ok: true,
      selection: {
        uri: doc.uri.toString(),
        fsPath: doc.uri.fsPath,
        text: doc.getText(editor.selection),
      },
    })
  })

  // ---- GET /vscode/workspace-folders ----
  // 当前 workspace 根目录列表（多根 workspace 支持）。没打开 workspace 时返空数组。
  app.get('/vscode/workspace-folders', (_req: Request, res: Response) => {
    const folders = vscode.workspace.workspaceFolders ?? []
    res.json({
      ok: true,
      folders: folders.map((f) => ({
        uri: f.uri.toString(),
        fsPath: f.uri.fsPath,
        name: f.name,
        index: f.index,
      })),
    })
  })

  // ---- POST /vscode/insert-snippet ----
  // 在目标编辑器里插入或替换文本。
  // body: { text, uri?, line?, character?, replace? }
  //   - text     : 要插入/替换的内容（必填）
  //   - uri      : 可选，指定目标文件；不传则取当前编辑器
  //   - line/character : 0-indexed 插入位置；不传则取当前光标
  //   - replace  : true 且当前选区非空时，替换选区而不是插入
  app.post('/vscode/insert-snippet', async (req: Request, res: Response) => {
    const body = (req.body ?? {}) as {
      text?: unknown
      uri?: unknown
      line?: unknown
      character?: unknown
      replace?: unknown
    }
    if (typeof body.text !== 'string') {
      res.status(400).json({ ok: false, error: 'body.text (string) required' })
      return
    }
    let editor: vscode.TextEditor | undefined
    if (typeof body.uri === 'string') {
      try {
        const targetUri = vscode.Uri.parse(body.uri)
        const doc = await vscode.workspace.openTextDocument(targetUri)
        editor = await vscode.window.showTextDocument(doc, { preview: false })
      } catch (err) {
        res.status(404).json({
          ok: false,
          error: `cannot open uri: ${err instanceof Error ? err.message : String(err)}`,
        })
        return
      }
    } else {
      editor = getCurrentTextEditor()
    }
    if (!editor) {
      res.status(409).json({ ok: false, error: 'no active editor; pass body.uri to specify target' })
      return
    }
    const doc = editor.document
    // 决定插入位置或替换 range
    let useRange: vscode.Range | null = null
    let position: vscode.Position
    if (body.replace === true && !editor.selection.isEmpty) {
      useRange = editor.selection
      position = editor.selection.start
    } else if (typeof body.line === 'number' && typeof body.character === 'number') {
      position = new vscode.Position(body.line, body.character)
    } else {
      position = editor.selection.active
    }
    const applied = await editor.edit((builder) => {
      if (useRange) {
        builder.replace(useRange, body.text as string)
      } else {
        builder.insert(position, body.text as string)
      }
    })
    res.json({
      ok: true,
      applied,
      uri: doc.uri.toString(),
      fsPath: doc.uri.fsPath,
      lineCount: doc.lineCount,
    })
  })

  // ---- POST /vscode/open-file ----
  // 让 IDE 打开指定文件，可选定位行列。
  // body: { uri? | fsPath?, line?, character?, preview?, viewColumn? }
  //   - uri / fsPath : 二选一，uri 是 file://… 格式，fsPath 是本地路径
  //   - line/character : 0-indexed，传了则打开后跳转并光标定位
  //   - preview  : 默认 false（持久打开、不是预览 tab）
  //   - viewColumn : 'active' (默认) | 'beside'（在旁边拆分窗口打开）
  app.post('/vscode/open-file', async (req: Request, res: Response) => {
    const body = (req.body ?? {}) as {
      uri?: unknown
      fsPath?: unknown
      line?: unknown
      character?: unknown
      preview?: unknown
      viewColumn?: unknown
    }
    let targetUri: vscode.Uri
    try {
      if (typeof body.uri === 'string') {
        targetUri = vscode.Uri.parse(body.uri)
      } else if (typeof body.fsPath === 'string') {
        targetUri = vscode.Uri.file(body.fsPath)
      } else {
        res.status(400).json({ ok: false, error: 'body.uri or body.fsPath required' })
        return
      }
    } catch (err) {
      res.status(400).json({
        ok: false,
        error: `invalid uri/fsPath: ${err instanceof Error ? err.message : String(err)}`,
      })
      return
    }
    try {
      const doc = await vscode.workspace.openTextDocument(targetUri)

      // viewColumn 智能选择：
      //   - 用户显式传 'beside' / 'active'：尊重用户
      //   - 默认（未传）：
      //       有 activeTextEditor（普通编辑器在前台）→ 用 Active（替换它，不动柳如烟 webview）
      //       没有（柳如烟 webview 在前台 / 没打开任何编辑器）→ 用 Beside（拆新列，不替换 webview）
      // 之前默认死锁在 Active：webview 在前台时 Active 就是 webview 那列 → 把柳如烟 webview 替换掉。
      // 这是 2026-05-13 用户报的 bug：「她打开了，但是打开的窗口把她自己替换了」。
      let resolvedColumn: vscode.ViewColumn
      if (body.viewColumn === 'beside') {
        resolvedColumn = vscode.ViewColumn.Beside
      } else if (body.viewColumn === 'active') {
        resolvedColumn = vscode.ViewColumn.Active
      } else {
        resolvedColumn = vscode.window.activeTextEditor
          ? vscode.ViewColumn.Active
          : vscode.ViewColumn.Beside
      }

      const showOptions: vscode.TextDocumentShowOptions = {
        preview: body.preview === true,
        viewColumn: resolvedColumn,
      }
      if (typeof body.line === 'number') {
        const ch = typeof body.character === 'number' ? body.character : 0
        const pos = new vscode.Position(body.line, ch)
        showOptions.selection = new vscode.Range(pos, pos)
      }
      await vscode.window.showTextDocument(doc, showOptions)
      res.json({
        ok: true,
        opened: true,
        uri: doc.uri.toString(),
        fsPath: doc.uri.fsPath,
        lineCount: doc.lineCount,
      })
    } catch (err) {
      res.status(404).json({
        ok: false,
        error: `cannot open: ${err instanceof Error ? err.message : String(err)}`,
      })
    }
  })

  // ---- POST /vscode/apply-edit ----
  // 让用户在 IDE 里 review 一个文件的完整修改 + 决定是否落盘。
  //
  // body: { uri? | fsPath?, newContent, description? }
  //   - uri / fsPath : 二选一定位目标文件
  //   - newContent   : 文件应变成的完整内容（full-file 模式）
  //   - description  : 可选一句话说明，显示在标题/通知
  //
  // **两种实现路径**（Phase 3 v2 引入）：
  //
  // - **v2 inline diff (DEFAULT)**: 走 applyEditV2.handleApplyEditV2
  //   buffer 立刻显示 proposedText（A 风格，Cascade UX）+ 每个 hunk 上方 CodeLens
  //   ✓ Accept / ✗ Reject 按钮，用户逐 hunk 决定。session 全部 resolved 时落盘。
  //   超时 600s 自动 abort 还原 baseline。
  //
  // - **v1 modal (FALLBACK, env LIURUYAN_INLINE_DIFF=0)**: 走原 vscode.diff view + modal
  //   工程量小但 UX 粗糙，留作 v2 出 bug 时的逃生舱。
  //
  // backend handler 侧 timeout 配 120s（v1）/ 600s（v2，跟 endpoint 内默认一致）。
  // [DIAG-2026-05-14] v1 路径加诊断日志定位 modal 没 block 的 bug
  const diagLog = (msg: string) => log(`[apply-edit][${Date.now()}] ${msg}`)
  app.post('/vscode/apply-edit', async (req: Request, res: Response) => {
    const body = (req.body ?? {}) as {
      uri?: unknown
      fsPath?: unknown
      newContent?: unknown
      description?: unknown
    }
    if (typeof body.newContent !== 'string') {
      res.status(400).json({ ok: false, error: 'body.newContent (string) required' })
      return
    }

    // Phase 3 v2 inline diff (DEFAULT): 委托给 applyEditV2 handler
    if (shouldUseInlineDiffV2()) {
      try {
        const v2Result = await handleApplyEditV2(body)
        // v2.1 #4 大文件降级 sentinel：v2 检测到 hunks 太多自愿降级 → 跳出 if 走 v1 path
        if (v2Result.fallbackToV1) {
          log(`[apply-edit] v2→v1 auto fallback: ${v2Result.reason}`)
          // fall through to v1 path below (不 return)
        } else if (v2Result.ok) {
          res.json(v2Result)
          return
        } else {
          // 错误响应保持跟 v1 同样的状态码语义：参数错 400, 内部错 500
          const isClientError = typeof v2Result.error === 'string' &&
            /required|invalid|must be/i.test(v2Result.error)
          res.status(isClientError ? 400 : 500).json(v2Result)
          return
        }
      } catch (err) {
        log(`[apply-edit-v2] uncaught: ${err instanceof Error ? err.message : String(err)}`)
        res.status(500).json({
          ok: false,
          error: `v2 handler crashed: ${err instanceof Error ? err.message : String(err)}`,
        })
        return
      }
    }

    // ---- v1 fallback (LIURUYAN_INLINE_DIFF=0 OR v2.1 #4 大文件降级) ----
    diagLog('v1 path (LIURUYAN_INLINE_DIFF=0 or v2 fallback)')
    let targetUri: vscode.Uri
    try {
      if (typeof body.uri === 'string') {
        targetUri = vscode.Uri.parse(body.uri)
      } else if (typeof body.fsPath === 'string') {
        targetUri = vscode.Uri.file(body.fsPath)
      } else {
        res.status(400).json({ ok: false, error: 'body.uri or body.fsPath required' })
        return
      }
    } catch (err) {
      res.status(400).json({
        ok: false,
        error: `invalid uri/fsPath: ${err instanceof Error ? err.message : String(err)}`,
      })
      return
    }

    const description = typeof body.description === 'string' ? body.description : ''
    const newContent = body.newContent
    const fileName = targetUri.fsPath.split(/[\\/]/).pop() || targetUri.toString()
    const diffTitle = description
      ? `柳如烟: ${fileName} — ${description}`
      : `柳如烟: ${fileName}`

    // 判定是否为「创建新文件」场景：能 openTextDocument 就是已存在文件
    let originalExists = false
    try {
      await vscode.workspace.openTextDocument(targetUri)
      originalExists = true
    } catch {
      originalExists = false
    }

    // 构造 right-side untitled URI 塞 newContent
    // 用 untitled: scheme + 文件名 query，让 VSCode diff view 自动按 languageId 高亮
    const rightUri = vscode.Uri.parse(
      `untitled:${targetUri.fsPath}.liuruyan-proposal`,
    )
    let rightDoc: vscode.TextDocument
    try {
      rightDoc = await vscode.workspace.openTextDocument(rightUri)
      // 清空并写入 newContent
      const edit = new vscode.WorkspaceEdit()
      const fullRange = new vscode.Range(
        rightDoc.positionAt(0),
        rightDoc.positionAt(rightDoc.getText().length),
      )
      edit.replace(rightUri, fullRange, newContent)
      await vscode.workspace.applyEdit(edit)
    } catch (err) {
      res.status(500).json({
        ok: false,
        error: `failed to prepare diff right document: ${err instanceof Error ? err.message : String(err)}`,
      })
      return
    }

    // 打开 diff view
    try {
      await vscode.commands.executeCommand(
        'vscode.diff',
        originalExists ? targetUri : vscode.Uri.parse('untitled:empty-original'),
        rightUri,
        diffTitle,
        { preview: true } satisfies vscode.TextDocumentShowOptions,
      )
    } catch (err) {
      res.status(500).json({
        ok: false,
        error: `failed to open diff view: ${err instanceof Error ? err.message : String(err)}`,
      })
      return
    }

    // 弹确认：modal=true 强制用户选（不会被其它 info 冲掉）
    // 注意 modal 会 block，直到用户点 Apply / Reject / Esc
    diagLog(`diff view opened, about to show modal (originalExists=${originalExists})`)
    let userChoice: 'apply' | 'reject' = 'reject'
    let pickedRaw: string | undefined = undefined
    let modalErr: string | null = null
    const modalStart = Date.now()
    try {
      const label = originalExists ? 'Apply Changes' : 'Create File'
      const picked = await vscode.window.showInformationMessage(
        originalExists
          ? `柳如烟 提议修改 ${fileName}${description ? `：${description}` : ''}。你要应用吗？`
          : `柳如烟 提议新建 ${fileName}${description ? `：${description}` : ''}。你要创建吗？`,
        { modal: true },
        label,
      )
      pickedRaw = picked
      if (picked === label) userChoice = 'apply'
    } catch (err) {
      modalErr = err instanceof Error ? err.message : String(err)
      userChoice = 'reject'
    }
    const modalElapsedMs = Date.now() - modalStart
    diagLog(
      `modal returned: elapsed=${modalElapsedMs}ms picked=${JSON.stringify(pickedRaw)} ` +
      `userChoice=${userChoice} err=${modalErr}`,
    )
    // 如果 modal 立刻返回（< 200ms）但 picked 等于 label，几乎肯定是 modal 被绕过了 —— 警告
    if (modalElapsedMs < 200 && userChoice === 'apply') {
      diagLog(
        `WARN: modal returned in ${modalElapsedMs}ms with apply choice — modal likely DID NOT BLOCK. ` +
        `pickedRaw=${JSON.stringify(pickedRaw)}. Forcing reject as safety net.`,
      )
      userChoice = 'reject'
    }

    // 落盘 / 关 diff
    let applied = false
    let errorMsg: string | null = null
    if (userChoice === 'apply') {
      try {
        const edit = new vscode.WorkspaceEdit()
        if (originalExists) {
          // 替换原文件全部内容
          const origDoc = await vscode.workspace.openTextDocument(targetUri)
          const fullRange = new vscode.Range(
            origDoc.positionAt(0),
            origDoc.positionAt(origDoc.getText().length),
          )
          edit.replace(targetUri, fullRange, newContent)
        } else {
          // 创建新文件
          edit.createFile(targetUri, {
            overwrite: false,
            ignoreIfExists: false,
            contents: new TextEncoder().encode(newContent),
          })
        }
        const ok = await vscode.workspace.applyEdit(edit)
        if (!ok) {
          errorMsg = 'workspace.applyEdit returned false'
        } else {
          // 确保磁盘持久化（applyEdit 只是 dirty，save 才落盘）
          if (originalExists) {
            const savedDoc = await vscode.workspace.openTextDocument(targetUri)
            await savedDoc.save()
          }
          applied = true
        }
      } catch (err) {
        errorMsg = `apply failed: ${err instanceof Error ? err.message : String(err)}`
      }
    }

    // 清理右侧 untitled document：切走 + revert 让它消失不留 dirty tab
    try {
      // 切 focus 到原文件（若存在）或关掉 untitled tab
      if (originalExists) {
        await vscode.window.showTextDocument(targetUri, { preview: false })
      }
      // 关闭 untitled（命令会关掉当前 active diff view 的 right side）
      await vscode.commands.executeCommand('workbench.action.closeActiveEditor')
    } catch {
      // 清理失败不影响主流程
    }

    res.json({
      ok: true,
      applied,
      uri: targetUri.toString(),
      fsPath: targetUri.fsPath,
      reason: applied
        ? (originalExists ? 'user accepted (file updated)' : 'user accepted (file created)')
        : errorMsg
          ? `apply attempted but failed: ${errorMsg}`
          : 'user rejected',
    })
  })

  // ---- POST /vscode/open-terminal ----
  // 创建或复用一个 VSCode terminal。
  //
  // body: { name?: string, cwd?: string, reuse?: boolean }
  //   - name   : 显示名，默认 "柳如烟"
  //   - cwd    : 工作目录绝对路径，不传沿用 VSCode 默认
  //   - reuse  : 默认 true。true = 若已有同名 entry 返回现有 terminal_id；false = 总是新建
  //
  // 响应: { ok, terminal_id, name, created }
  //   - created = 本次是否真正新建（false 表示复用了已有）
  app.post('/vscode/open-terminal', async (req: Request, res: Response) => {
    const body = (req.body ?? {}) as {
      name?: unknown
      cwd?: unknown
      reuse?: unknown
    }
    try {
      const r = openOrReuseTerminal({
        name: typeof body.name === 'string' ? body.name : undefined,
        cwd: typeof body.cwd === 'string' ? body.cwd : undefined,
        reuse: typeof body.reuse === 'boolean' ? body.reuse : undefined,
      })
      r.terminal.show(true) // preserveFocus=true 不抢 webview 焦点
      res.json({
        ok: true,
        terminal_id: r.id,
        name: r.name,
        created: r.created,
      })
    } catch (err) {
      res.status(500).json({
        ok: false,
        error: `failed to open terminal: ${err instanceof Error ? err.message : String(err)}`,
      })
    }
  })

  // ---- POST /vscode/run-command-in-terminal ----
  // 在指定 terminal 跑命令。**output 不保证回取**（取决于 shell integration 是否可用）。
  //
  // body: {
  //   terminal_id?: string,    // 不传 → 自动 open "柳如烟" terminal reuse=true
  //   command: string,         // 必填
  //   wait_for_output?: boolean, // 默认 true。false → 直接 sendText + 立即返回
  //   timeout?: number,        // 秒，默认 60。仅对 wait_for_output=true 有效
  // }
  //
  // 响应:
  //   能捕获 output: { ok, sent: true, output, exitCode, captured: true, terminal_id }
  //   不能捕获:     { ok, sent: true, output: null, captured: false, reason, terminal_id }
  //   发送失败:     400 / 500
  app.post('/vscode/run-command-in-terminal', async (req: Request, res: Response) => {
    const body = (req.body ?? {}) as {
      terminal_id?: unknown
      command?: unknown
      wait_for_output?: unknown
      timeout?: unknown
    }
    if (typeof body.command !== 'string' || !body.command.trim()) {
      res.status(400).json({ ok: false, error: 'body.command (non-empty string) required' })
      return
    }
    const command = body.command
    const waitForOutput = body.wait_for_output !== false // 默认 true
    const timeoutSec =
      typeof body.timeout === 'number' && body.timeout > 0 ? Math.min(body.timeout, 600) : 60

    // 找 / 创建 terminal
    let terminal: vscode.Terminal | null = null
    let terminalId: string | null = null
    if (typeof body.terminal_id === 'string' && body.terminal_id) {
      terminal = getTerminal(body.terminal_id)
      if (!terminal) {
        res.status(404).json({
          ok: false,
          error: `terminal_id '${body.terminal_id}' not found or already closed`,
        })
        return
      }
      terminalId = body.terminal_id
    } else {
      const r = openOrReuseTerminal({ reuse: true })
      terminal = r.terminal
      terminalId = r.id
    }

    // fire-and-forget 模式
    if (!waitForOutput) {
      try {
        terminal.show(true)
        terminal.sendText(command, true) // addNewLine=true 自动按回车
        res.json({
          ok: true,
          sent: true,
          output: null,
          captured: false,
          reason: 'wait_for_output=false, fire-and-forget mode',
          terminal_id: terminalId,
        })
      } catch (err) {
        res.status(500).json({
          ok: false,
          error: `sendText failed: ${err instanceof Error ? err.message : String(err)}`,
        })
      }
      return
    }

    // 带 output 捕获模式：先尝试 shell integration，不可用就降级 sendText
    try {
      const result = await executeWithShellIntegration(
        terminal,
        command,
        2000, // 等 shell integration 激活最多 2s
        timeoutSec * 1000,
      )
      if (result) {
        res.json({
          ok: true,
          sent: true,
          output: result.output,
          exitCode: result.exitCode,
          captured: true,
          terminal_id: terminalId,
        })
        return
      }
      // 降级：shell integration 不可用
      terminal.sendText(command, true)
      res.json({
        ok: true,
        sent: true,
        output: null,
        captured: false,
        reason:
          'Shell integration unavailable (VSCode < 1.93 or shell not supported). ' +
          'Command was sent fire-and-forget. For guaranteed output capture, use backend execute_command tool.',
        terminal_id: terminalId,
      })
    } catch (err) {
      res.status(500).json({
        ok: false,
        error: `execute failed: ${err instanceof Error ? err.message : String(err)}`,
      })
    }
  })

  // ---- GET /vscode/diagnostics ----
  // 拉诊断信息。默认返当前编辑器的，带 ?uri=... 返指定文件，带 ?all=1 返全 workspace。
  app.get('/vscode/diagnostics', (req: Request, res: Response) => {
    const all = req.query.all === '1' || req.query.all === 'true'
    const uriParam = typeof req.query.uri === 'string' ? req.query.uri : undefined
    const items: { uri: vscode.Uri; diag: vscode.Diagnostic }[] = []
    if (all) {
      for (const [uri, diags] of vscode.languages.getDiagnostics()) {
        for (const d of diags) items.push({ uri, diag: d })
      }
    } else {
      let targetUri: vscode.Uri | undefined
      if (uriParam) {
        try {
          targetUri = vscode.Uri.parse(uriParam)
        } catch {
          targetUri = undefined
        }
      } else {
        const editor = getCurrentTextEditor()
        targetUri = editor?.document.uri
      }
      if (targetUri) {
        for (const d of vscode.languages.getDiagnostics(targetUri)) {
          items.push({ uri: targetUri, diag: d })
        }
      }
    }
    res.json({
      ok: true,
      diagnostics: items.map(({ uri, diag }) => normalizeDiagnostic(uri, diag)),
    })
  })
}
