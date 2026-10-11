/**
 * Windows 防黑窗 Node.js 钩子 (Silent Subprocess Hook)
 * 
 * 背景：
 * Node.js 在 Windows 上调用 child_process 的 spawn / exec / execFile / spawnSync / execFileSync 时，
 * 默认 windowsHide 为 false。当父进程（如 MCP Server、后台 daemon、Tauri sidecar）本身没有控制台时，
 * 每一个未显式设置 windowsHide: true 的子进程（如 git.exe、rg.exe、tar.exe 等）都会在 Windows 上弹出一个黑控制台窗口。
 * 尤其像代码分析与索引类 MCP 服务，在启动索引和文件变化监听时会高频调用 git rev-parse, git ls-files, git diff,
 * 导致用户桌面疯狂闪烁写着 "git" 的黑窗口。
 * 
 * 作用：
 * 由 backend/mcp/stateful_client.py 在 Windows 下通过 NODE_OPTIONS="--require <this_file>" 注入给所有 Node 编写的
 * MCP 客户端与服务进程。拦截所有 child_process 派生调用，为没有显式指定 windowsHide 的参数默认注入 windowsHide: true。
 */
'use strict';

if (process.platform === 'win32') {
  try {
    const cp = require('node:child_process');

    function patchSpawn(orig) {
      if (typeof orig !== 'function') return orig;
      return function(file, args, options) {
        let a = args;
        let o = options;
        if (typeof a === 'object' && !Array.isArray(a) && a !== null) {
          o = a;
          a = [];
        }
        o = Object.assign({}, o);
        if (o.windowsHide === undefined) {
          o.windowsHide = true;
        }
        return orig.call(this, file, a, o);
      };
    }

    function patchExecFile(orig) {
      if (typeof orig !== 'function') return orig;
      return function(file, args, options, callback) {
        let a = args;
        let o = options;
        let cb = callback;
        if (typeof a === 'function') {
          cb = a;
          a = [];
          o = {};
        } else if (typeof a === 'object' && !Array.isArray(a) && a !== null) {
          cb = typeof o === 'function' ? o : cb;
          o = a;
          a = [];
        } else if (typeof o === 'function') {
          cb = o;
          o = {};
        }
        o = Object.assign({}, o);
        if (o.windowsHide === undefined) {
          o.windowsHide = true;
        }
        return orig.call(this, file, a, o, cb);
      };
    }

    function patchExec(orig) {
      if (typeof orig !== 'function') return orig;
      return function(command, options, callback) {
        let o = options;
        let cb = callback;
        if (typeof o === 'function') {
          cb = o;
          o = {};
        }
        o = Object.assign({}, o);
        if (o.windowsHide === undefined) {
          o.windowsHide = true;
        }
        return orig.call(this, command, o, cb);
      };
    }

    cp.spawn = patchSpawn(cp.spawn);
    cp.spawnSync = patchSpawn(cp.spawnSync);
    cp.execFile = patchExecFile(cp.execFile);
    cp.execFileSync = patchExecFile(cp.execFileSync);
    cp.exec = patchExec(cp.exec);
    cp.execSync = patchExec(cp.execSync);
  } catch (err) {
    // 保护性静默，不得破坏 Node 正常运行
  }
}
