# 柳如烟 VSCode 插件

把柳如烟（multi-agent / MCP / vision / 自定义 provider）装进 VSCode / Windsurf / Cursor。

> 阶段 1 (MVP)：装插件后**需要先开柳如烟 backend（运行项目根 `启动.bat`）**。阶段 2 才做"装插件即可"的 auto-spawn。
>
> 设计依据见 `./_docs\vscode_extension_plan.md`。
> 集成铁律见 `./_docs\ide_integration_principles.md`。

---

## 当前进度（2026-05-12）

- [x] T1：插件骨架（本目录）
- [x] T2：frontend `src/runtime/` 抽象层占位
- [ ] T3：改造 `frontend/src/App.tsx` Tauri API 调用到 runtime 抽象
- [ ] T4：验证 Tauri 桌面端不退化
- [ ] T5：插件内置 HTTP server + CSRF
- [ ] T6：WebviewPanel + iframe 加载 frontend
- [ ] T7：Extension Development Host 跑通
- [ ] T8：反向代理到 backend :8001
- [ ] T9：`frontend/src/runtime/vscode.ts` 实现
- [ ] T10：`/vscode/*` 桥接端点（5-7 个）
- [ ] T11：烟雾测试
- [ ] T12-T14：文档 / 验收 / handover

详见 `./_docs\vscode_extension_plan.md` §12。

---

## 本地开发

### 前置

- Node 18+ / pnpm 或 npm
- VSCode 1.85+ 或 Windsurf
- 柳如烟 backend 能跑（`启动.bat` 至少能起后端那一半）

### 安装依赖

```powershell
cd vscode-extension
npm install
```

### 编译

```powershell
npm run compile      # 一次性
npm run watch        # 持续监听
```

### 调试（Extension Development Host）

1. 用 VSCode / Windsurf 打开 `vscode-extension/` 目录
2. 按 `F5` 启动 "Run Extension"
3. 会拉起一个 Extension Development Host 窗口
4. 在 Host 窗口里命令面板（Ctrl+Shift+P）→ `Liuruyan: Open Panel`
5. （阶段 1）会显示提示信息；T5 之后会真正打开 webview

### 打包

```powershell
npm run package      # 出 .vsix 文件
```

`.vsix` 可以拖进 Windsurf / VSCode / Cursor → `Extensions: Install from VSIX...` 安装。

---

## 设计契约（接手前必读）

### 项目独立性（最重要）

这个插件是柳如烟的**分支**，不是主项目本身。所有改动必须遵守：

1. **不修改 `backend/` 任何代码** —— 插件只是 HTTP 客户端
2. **不修改 `frontend/` 既有业务代码** —— 只允许新增 `frontend/src/runtime/` 抽象层
3. 插件 npm 依赖独立 `vscode-extension/package.json`，不污染 `frontend/package.json`
4. 删除 `vscode-extension/` 目录 + 删除 `frontend/src/runtime/vscode.ts` → 主项目零退化

完整隔离边界见 `./_docs\vscode_extension_plan.md` §4。

### 不允许做的事（红线摘录）

- 不在插件里跑 LLM 调用 / agent 编排（业务逻辑全在 backend）
- 不让前端代码 `import 'vscode'` 或调 `acquireVsCodeApi()`（违反铁律 1）
- 不写死端口（铁律 2，CSRF 强制铁律 3，SSE 不 WebSocket 铁律 7）
- 不把 backend.exe 打进 .vsix（阶段 1 不需要，阶段 2 单独评估）

完整红线见 `./_docs\vscode_extension_plan.md` §11。

---

## 目录结构

```
vscode-extension/
├── package.json          # 插件 manifest
├── tsconfig.json
├── .gitignore
├── .vscodeignore         # 打包时排除
├── README.md             # 本文件
└── src/
    └── extension.ts      # 入口（activate / deactivate）
```

T5+ 会扩展为：

```
src/
├── extension.ts          # 入口
├── config.ts             # 端口 / csrf 生成
├── server/               # HTTP server + CSRF + 反向代理
├── bridges/              # VSCode API 桥接（editor / workspace / diagnostics）
└── webview/              # WebviewPanel 创建 + html shell
```

---

## 相关文档

| 用途 | 文档 |
|---|---|
| 设计起点 / 决策溯源 | `./_docs\vscode_extension_plan.md` |
| IDE 集成 7 铁律 | `./_docs\ide_integration_principles.md` |
| Windsurf 插件逆向参考 | `./_docs\research_windsurf_plugin.md` |
| 主项目工程地图 | `./_docs\CODE_MAP.md` |
| Tauri 端架构对照 | `./_docs\Tauri集成方案说明.md` |
