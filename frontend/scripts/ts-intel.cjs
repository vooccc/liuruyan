#!/usr/bin/env node
/**
 * ts-intel.cjs — TypeScript code intelligence CLI for the backend LSP tool.
 *
 * Protocol:
 *   Input:  argv[2] = JSON string {op, file?, line?, character?, query?, newName?, tsconfig?}
 *   Output: stdout JSON {success, ...}  (errors go in stdout too as {success:false, error})
 *
 * Operations:
 *   goToDefinition / findReferences / hover / documentSymbol / workspaceSymbol /
 *   incomingCalls / outgoingCalls / rename
 *
 * Cold start ~400-800ms on this repo; that's fine for a non-hot-path tool.
 */
"use strict";

const fs = require("fs");
const path = require("path");

let ts;
try {
  ts = require("typescript");
} catch (e) {
  process.stdout.write(JSON.stringify({ success: false, error: "typescript not installed in frontend/node_modules" }));
  process.exit(0);
}

// -----------------------------------------------------------------------------
// arg state — populated by either one-shot main() or daemon request loop.
// `args` is mutable so daemon mode can reassign per request without rebuilding
// the OPS dispatch table. `tsconfigPath` defaults but can be overridden by the
// first request's `tsconfig` field.
// -----------------------------------------------------------------------------
const FRONTEND_DIR = path.resolve(__dirname, "..");
let tsconfigPath = path.join(FRONTEND_DIR, "tsconfig.json");
let args = null;

// -----------------------------------------------------------------------------
// LanguageService construction
// -----------------------------------------------------------------------------
function loadProgram() {
  const configRead = ts.readConfigFile(tsconfigPath, ts.sys.readFile);
  if (configRead.error) {
    return { error: ts.flattenDiagnosticMessageText(configRead.error.messageText, "\n") };
  }
  const parsed = ts.parseJsonConfigFileContent(
    configRead.config,
    ts.sys,
    path.dirname(tsconfigPath)
  );
  if (parsed.errors && parsed.errors.length) {
    const msg = parsed.errors.map(e => ts.flattenDiagnosticMessageText(e.messageText, "\n")).join("; ");
    if (!parsed.fileNames.length) return { error: "tsconfig parse: " + msg };
  }
  // include the explicit target file even if outside `include`
  const fileNames = parsed.fileNames.slice();
  if (args.file) {
    const abs = path.resolve(args.file);
    if (fs.existsSync(abs) && !fileNames.includes(abs)) fileNames.push(abs);
  }
  const options = parsed.options;

  const host = {
    getScriptFileNames: () => fileNames,
    getScriptVersion: () => "1",
    getScriptSnapshot: (f) => {
      try { return ts.ScriptSnapshot.fromString(fs.readFileSync(f, "utf8")); }
      catch { return undefined; }
    },
    getCurrentDirectory: () => path.dirname(tsconfigPath),
    getCompilationSettings: () => options,
    getDefaultLibFileName: (o) => ts.getDefaultLibFilePath(o),
    fileExists: ts.sys.fileExists,
    readFile: ts.sys.readFile,
    readDirectory: ts.sys.readDirectory,
    directoryExists: ts.sys.directoryExists,
    getDirectories: ts.sys.getDirectories,
  };

  const service = ts.createLanguageService(host, ts.createDocumentRegistry());
  return { service, fileNames, options };
}

// -----------------------------------------------------------------------------
// helpers
// -----------------------------------------------------------------------------
function fileSnapshot(service, file) {
  const program = service.getProgram();
  if (!program) return null;
  return program.getSourceFile(file) || null;
}

function posOf(service, file, line1, char1) {
  // input 1-based, ts uses 0-based
  const sf = fileSnapshot(service, file);
  if (!sf) return null;
  const line0 = Math.max(0, line1 - 1);
  const char0 = Math.max(0, char1 - 1);
  try {
    return sf.getPositionOfLineAndCharacter(line0, char0);
  } catch {
    return null;
  }
}

function lineCharOf(file, posOrSourceFile, pos) {
  // returns {line:1-based, character:1-based}
  let sf;
  if (typeof posOrSourceFile === "object" && posOrSourceFile.getLineAndCharacterOfPosition) {
    sf = posOrSourceFile;
  } else {
    pos = posOrSourceFile;
    try {
      const text = fs.readFileSync(file, "utf8");
      sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
    } catch { return { line: 0, character: 0 }; }
  }
  const lc = sf.getLineAndCharacterOfPosition(pos);
  return { line: lc.line + 1, character: lc.character + 1 };
}

function navKindToOurKind(kind) {
  const map = {
    "class": "class",
    "interface": "interface",
    "function": "function",
    "method": "method",
    "var": "variable", "let": "variable", "const": "variable",
    "property": "property",
    "enum": "enum",
    "type": "type",
    "module": "module",
    "alias": "alias",
  };
  return map[kind] || kind || "unknown";
}

// -----------------------------------------------------------------------------
// operations
// -----------------------------------------------------------------------------
function opGoToDefinition(service) {
  const file = path.resolve(args.file);
  const pos = posOf(service, file, args.line, args.character);
  if (pos === null) return { success: false, error: "file not in program or invalid position" };
  const defs = service.getDefinitionAtPosition(file, pos) || [];
  const locations = defs.map(d => {
    const lc = lineCharOf(d.fileName, d.textSpan.start);
    return {
      file: d.fileName,
      line: lc.line,
      character: lc.character,
      kind: navKindToOurKind(d.kind),
      name: d.name,
      containerName: d.containerName || null,
    };
  });
  return { success: true, locations };
}

function opFindReferences(service) {
  const file = path.resolve(args.file);
  const pos = posOf(service, file, args.line, args.character);
  if (pos === null) return { success: false, error: "file not in program or invalid position" };
  const refs = service.getReferencesAtPosition(file, pos) || [];
  const out = refs.map(r => {
    const lc = lineCharOf(r.fileName, r.textSpan.start);
    return {
      file: r.fileName,
      line: lc.line,
      character: lc.character,
      isDefinition: !!r.isDefinition,
      isWriteAccess: !!r.isWriteAccess,
    };
  });
  return { success: true, references: out, count: out.length };
}

function opHover(service) {
  const file = path.resolve(args.file);
  const pos = posOf(service, file, args.line, args.character);
  if (pos === null) return { success: false, error: "file not in program or invalid position" };
  const info = service.getQuickInfoAtPosition(file, pos);
  if (!info) return { success: true, hover_text: "" };
  const sig = (info.displayParts || []).map(p => p.text).join("");
  const doc = (info.documentation || []).map(p => p.text).join("");
  const tags = (info.tags || []).map(t => `@${t.name}${t.text ? " " + t.text.map(p => p.text).join("") : ""}`).join("\n");
  const parts = [];
  if (sig) parts.push("```typescript\n" + sig + "\n```");
  if (doc) parts.push(doc);
  if (tags) parts.push(tags);
  return { success: true, hover_text: parts.join("\n\n"), kind: navKindToOurKind(info.kind) };
}

function flattenNavTree(tree, file, container, out) {
  if (!tree) return;
  // Skip the synthetic <global> root
  if (tree.kind && tree.kind !== "<global>" && tree.text && tree.text !== "<global>") {
    const span = (tree.spans && tree.spans[0]) || tree.nameSpan || null;
    if (span) {
      const lc = lineCharOf(file, span.start);
      out.push({
        name: tree.text,
        kind: navKindToOurKind(tree.kind),
        line: lc.line,
        character: lc.character,
        container: container || null,
      });
    }
  }
  const newContainer =
    (tree.kind === "class" || tree.kind === "interface" || tree.kind === "module")
      ? tree.text
      : container;
  if (tree.childItems) for (const c of tree.childItems) flattenNavTree(c, file, newContainer, out);
}

function opDocumentSymbol(service) {
  const file = path.resolve(args.file);
  if (!fileSnapshot(service, file)) {
    return { success: false, error: "file not in program: " + file };
  }
  const tree = service.getNavigationTree(file);
  const out = [];
  flattenNavTree(tree, file, null, out);
  return { success: true, symbols: out, count: out.length };
}

function opWorkspaceSymbol(service) {
  const q = args.query || "";
  const items = service.getNavigateToItems(q, 200, undefined, false) || [];
  const out = items.map(it => {
    const lc = lineCharOf(it.fileName, it.textSpan.start);
    return {
      name: it.name,
      kind: navKindToOurKind(it.kind),
      file: it.fileName,
      line: lc.line,
      character: lc.character,
      container: it.containerName || null,
      matchKind: it.matchKind, // exact / prefix / substring / camelCase
    };
  });
  return { success: true, symbols: out, count: out.length, truncated: out.length === 200 };
}

function opIncomingCalls(service) {
  const file = path.resolve(args.file);
  const pos = posOf(service, file, args.line, args.character);
  if (pos === null) return { success: false, error: "file not in program or invalid position" };
  const items = service.provideCallHierarchyIncomingCalls(file, pos) || [];
  const callers = items.map(it => {
    const fromFile = it.from.file;
    const fromLc = lineCharOf(fromFile, it.from.selectionSpan.start);
    return {
      caller_name: it.from.name,
      caller_kind: navKindToOurKind(it.from.kind),
      caller_file: fromFile,
      caller_line: fromLc.line,
      caller_container: it.from.containerName || null,
      call_sites: (it.fromSpans || []).map(s => {
        const lc = lineCharOf(fromFile, s.start);
        return { line: lc.line, character: lc.character };
      }),
    };
  });
  return { success: true, callers, count: callers.length };
}

function opOutgoingCalls(service) {
  const file = path.resolve(args.file);
  const pos = posOf(service, file, args.line, args.character);
  if (pos === null) return { success: false, error: "file not in program or invalid position" };
  const items = service.provideCallHierarchyOutgoingCalls(file, pos) || [];
  const calls = items.map(it => {
    const targetFile = it.to.file;
    const targetLc = lineCharOf(targetFile, it.to.selectionSpan.start);
    return {
      name: it.to.name,
      target_kind: navKindToOurKind(it.to.kind),
      target_file: targetFile,
      target_line: targetLc.line,
      call_sites: (it.fromSpans || []).map(s => {
        const lc = lineCharOf(file, s.start);
        return { line: lc.line, character: lc.character };
      }),
    };
  });
  return { success: true, calls, count: calls.length };
}

function opGoToImplementation(service) {
  const file = path.resolve(args.file);
  const pos = posOf(service, file, args.line, args.character);
  if (pos === null) return { success: false, error: "file not in program or invalid position" };
  // TS LanguageService 在 position 不是 interface / abstract / generic 等可寻"实现"
  // 的符号时会抛内部 TypeError（getImplementationsAtPosition: Cannot read 'kind' of undefined）。
  // 这是 TS 实现细节而非用户错误，按"没有实现"处理更友好。
  let impls;
  try {
    impls = service.getImplementationAtPosition(file, pos) || [];
  } catch (e) {
    return {
      success: true,
      locations: [],
      message: "No implementations found at this position (symbol is not an interface or abstract method).",
    };
  }
  const locations = impls.map(d => {
    const lc = lineCharOf(d.fileName, d.textSpan.start);
    return {
      file: d.fileName,
      line: lc.line,
      character: lc.character,
      kind: navKindToOurKind(d.kind),
    };
  });
  return { success: true, locations };
}

function opDiagnostics(service) {
  const file = path.resolve(args.file);
  if (!fileSnapshot(service, file)) return { success: false, error: "file not in program: " + file };
  const program = service.getProgram();
  const sf = program ? program.getSourceFile(file) : null;
  const syn = service.getSyntacticDiagnostics(file) || [];
  const sem = service.getSemanticDiagnostics(file) || [];
  const all = [...syn, ...sem];
  // category: 0=warning 1=error 2=suggestion 3=message
  const sevMap = ["warning", "error", "suggestion", "message"];
  const items = all.map(d => {
    const dsf = d.file || sf;
    const startPos = d.start || 0;
    const endPos = startPos + (d.length || 0);
    const startLc = dsf ? dsf.getLineAndCharacterOfPosition(startPos) : { line: 0, character: 0 };
    const endLc = dsf ? dsf.getLineAndCharacterOfPosition(endPos) : startLc;
    return {
      line: startLc.line + 1,
      character: startLc.character + 1,
      end_line: endLc.line + 1,
      end_character: endLc.character + 1,
      severity: sevMap[d.category] || "error",
      code: d.code,
      message: ts.flattenDiagnosticMessageText(d.messageText, "\n"),
      source: "tsc",
      file: (d.file && d.file.fileName) || file,
    };
  });
  return { success: true, diagnostics: items, count: items.length, file };
}

function opRename(service) {
  const file = path.resolve(args.file);
  const pos = posOf(service, file, args.line, args.character);
  if (pos === null) return { success: false, error: "file not in program or invalid position" };
  const newName = args.newName;
  if (!newName) return { success: false, error: "newName is required" };
  const locs = service.findRenameLocations(file, pos, false, false, {}) || [];
  const byFile = {};
  for (const l of locs) {
    if (!byFile[l.fileName]) byFile[l.fileName] = [];
    const lc = lineCharOf(l.fileName, l.textSpan.start);
    byFile[l.fileName].push({ line: lc.line, character: lc.character, new_name: newName });
  }
  return {
    success: true,
    changes: byFile,
    files_affected: Object.keys(byFile).length,
    total_changes: locs.length,
  };
}

// -----------------------------------------------------------------------------
// dispatch
// -----------------------------------------------------------------------------
const OPS = {
  goToDefinition: opGoToDefinition,
  findReferences: opFindReferences,
  hover: opHover,
  documentSymbol: opDocumentSymbol,
  workspaceSymbol: opWorkspaceSymbol,
  incomingCalls: opIncomingCalls,
  outgoingCalls: opOutgoingCalls,
  rename: opRename,
  goToImplementation: opGoToImplementation,
  diagnostics: opDiagnostics,
};

function main() {
  const op = args.op;
  const handler = OPS[op];
  if (!handler) {
    return { success: false, error: "unknown op: " + op + "; supported: " + Object.keys(OPS).join(", ") };
  }
  const loaded = loadProgram();
  if (loaded.error) return { success: false, error: loaded.error };
  try {
    return handler(loaded.service);
  } catch (e) {
    return { success: false, error: "ts-intel " + op + " failed: " + (e && e.stack || String(e)) };
  }
}

// -----------------------------------------------------------------------------
// Daemon mode — persistent LanguageService across requests, ~30x speed-up.
//
// Protocol (line-delimited JSON, stdin/stdout):
//   request:  {"id": "<str>", "op": "<op>", ...args}
//   response: {"id": "<str>", "success": bool, ...result}
//
// Lifecycle:
//   - On startup, emit {"ready": true, "pid": ..., "idle_ms": ...} once.
//   - Build LanguageService lazily on first request.
//   - Reset idle timer on each request; exit(0) after IDLE_MS of inactivity.
//   - Stdin EOF → exit(0).
//
// File-version: uses mtime so TS picks up disk changes between requests.
// -----------------------------------------------------------------------------
let daemonService = null;
let daemonFileNames = null;
let daemonOptions = null;

function buildDaemonService() {
  const configRead = ts.readConfigFile(tsconfigPath, ts.sys.readFile);
  if (configRead.error) {
    throw new Error(ts.flattenDiagnosticMessageText(configRead.error.messageText, "\n"));
  }
  const parsed = ts.parseJsonConfigFileContent(
    configRead.config, ts.sys, path.dirname(tsconfigPath)
  );
  daemonFileNames = parsed.fileNames.slice();
  daemonOptions = parsed.options;

  const host = {
    getScriptFileNames: () => daemonFileNames,
    getScriptVersion: (f) => {
      try { return String(fs.statSync(f).mtimeMs); }
      catch { return "0"; }
    },
    getScriptSnapshot: (f) => {
      try { return ts.ScriptSnapshot.fromString(fs.readFileSync(f, "utf8")); }
      catch { return undefined; }
    },
    getCurrentDirectory: () => path.dirname(tsconfigPath),
    getCompilationSettings: () => daemonOptions,
    getDefaultLibFileName: (o) => ts.getDefaultLibFilePath(o),
    fileExists: ts.sys.fileExists,
    readFile: ts.sys.readFile,
    readDirectory: ts.sys.readDirectory,
    directoryExists: ts.sys.directoryExists,
    getDirectories: ts.sys.getDirectories,
  };
  daemonService = ts.createLanguageService(host, ts.createDocumentRegistry());
}

function handleDaemonRequest(req) {
  args = req;
  if (req.tsconfig && !daemonService) {
    tsconfigPath = path.resolve(req.tsconfig);
  }
  const id = req.id;
  try {
    if (!daemonService) buildDaemonService();
  } catch (e) {
    return { id, success: false, error: "build LanguageService failed: " + String(e.message || e) };
  }
  // Dynamically include the target file if missing from tsconfig include glob.
  if (args.file) {
    const abs = path.resolve(args.file);
    if (fs.existsSync(abs) && !daemonFileNames.includes(abs)) {
      daemonFileNames.push(abs);
    }
  }
  const op = req.op;
  const handler = OPS[op];
  if (!handler) {
    return { id, success: false, error: "unknown op: " + op + "; supported: " + Object.keys(OPS).join(", ") };
  }
  try {
    const result = handler(daemonService);
    return Object.assign({ id }, result);
  } catch (e) {
    return { id, success: false, error: "ts-intel " + op + " failed: " + (e && e.stack || String(e)) };
  }
}

function runDaemon() {
  const IDLE_MS = parseInt(process.env.TS_INTEL_IDLE_MS || "120000", 10);
  let idleTimer = null;
  function resetIdle() {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { process.exit(0); }, IDLE_MS);
  }
  resetIdle();

  let buf = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf("\n")) !== -1) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (!line.trim()) continue;
      resetIdle();
      let req;
      try { req = JSON.parse(line); }
      catch (e) {
        process.stdout.write(JSON.stringify({ success: false, error: "invalid JSON: " + e.message }) + "\n");
        continue;
      }
      const resp = handleDaemonRequest(req);
      process.stdout.write(JSON.stringify(resp) + "\n");
    }
  });
  process.stdin.on("end", () => { process.exit(0); });
  process.stdin.on("error", () => { process.exit(1); });

  // Ready signal — Python client awaits this before sending the first request.
  process.stdout.write(JSON.stringify({ ready: true, pid: process.pid, idle_ms: IDLE_MS }) + "\n");
}

// -----------------------------------------------------------------------------
// Entry dispatch: --daemon for long-lived, argv[2] JSON for legacy one-shot.
// -----------------------------------------------------------------------------
if (process.argv.includes("--daemon")) {
  runDaemon();
} else {
  const raw = process.argv[2];
  if (!raw) {
    process.stdout.write(JSON.stringify({ success: false, error: "missing JSON argv[2]" }));
    process.exit(0);
  }
  try { args = JSON.parse(raw); }
  catch (e) {
    process.stdout.write(JSON.stringify({ success: false, error: "invalid JSON argv[2]: " + e.message }));
    process.exit(0);
  }
  if (args.tsconfig) tsconfigPath = path.resolve(args.tsconfig);
  const result = main();
  process.stdout.write(JSON.stringify(result));
}
