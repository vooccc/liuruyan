# 柳如烟 (Ruyan AI) · 赛博合伙人与自主智能体生态

<p align="center">
  <b>不仅具备强大的递归多智能体（Subagent Swarm）执行力，更是一支有灵魂、有深度记忆、支持跨平台协同的私人数字生命团队。</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Desktop-Tauri_v2-blue?logo=tauri" alt="Tauri v2" />
  <img src="https://img.shields.io/badge/Frontend-React_18_%7C_TypeScript-61dafb?logo=react" alt="React" />
  <img src="https://img.shields.io/badge/Backend-Python_3.11_Harness-yellow?logo=python" alt="Python" />
  <img src="https://img.shields.io/badge/Protocol-MCP_%7C_Claude_Skills-purple" alt="MCP & Skills" />
  <img src="https://img.shields.io/badge/License-LGPL_3.0-blue" alt="License" />
</p>

---

## 🌟 核心特色与杀手级优势

1. **🧠 真·数字生命灵魂体系（不可变基因 + 长期记忆图谱 + 自主情感反思日记）**
   - 拒绝千篇一律、聊过即忘的塑料 AI。角色拥有底层**不可变性格基因 (GENOME)** 与**动态演化心智**；长短期记忆基于本地纯文本知识图谱深度索引；夜间自主撰写带情绪起伏与认知演进的**私人反思日记**，真正与用户共同成长。
2. **🎭 独创「角色装备槽 (Persona Equipment)」与四维能力工坊**
   - 彻底解决工具全量注入带来的 Token 爆炸与模型路由幻觉。像 RPG 角色配置装备一样，按需为每个角色自由插拔 **Claude Code Skills 技能包**、**标准 MCP 服务**、**原生 Python Tools** 与 **外部 ACP 智能体**。角色专注度提升 300%，调用零冗余。
3. **👥 多智能体圆桌议事厅 (Roundtable Multi-Agent Swarm)**
   - 打破传统单角色对话瓶颈。让架构师、全栈工程师、产品经理、安全审计员等多个异构智能体同坐一张圆桌，自主展开观点碰撞、协同头脑风暴与代码交叉评审，支持全流程人机无缝插话。
4. **⚡ 自主递归 Subagent 任务编排引擎 + 熔断守护**
   - 前台交互与后台重型任务物理隔离。复杂任务自动委派至探索型 (Explore)、规划型 (Plan) 与执行型 Subagent 独立沙箱中递归推进，不污染主会话上下文；内置 **Tool Loop Guard** 循环探测器，彻底告别死循环与 Token 跑飞。
5. **💻 VSCode 深度原生双向热联动**
   - 专为开发者打造的桌面与 IDE 桥梁。配备官方开源 VSCode 伴侣插件，实现桌面客户端与代码编辑器的实时双向通信：支持文件定位跳转、代码内联即时 Diff 预览与一键采纳应用，丝滑切入日常研发工作流。
6. **📲 全天候多端 IM 触达与反向远程唤醒（微信 / 企微 / 飞书 / 钉钉 / Telegram / Slack）**
   - 摆脱物理工位限制。出门在外通过手机微信、飞书或钉钉向专属机器人发送自然语言指令，电脑端将自动被远程唤醒并调度后台 Subagent 执行复杂长链任务，任务完成后主动将总结与交付物推送到手机。
7. **🚀 Tauri v2 + Rust 极速底座 & 100% 本地数据主权**
   - 告别笨重的 Electron 内存消耗（内存仅数十兆，秒级启动）。所有记忆图谱、聊天日志、角色基因和 API 密钥全部保存在用户本地磁盘的清晰 Markdown/JSON 目录中，零遥测、零云端中转，你的数据主权 100% 归属于你。

---

## 🛠️ 架构一览

```text
┌─────────────────────────────────────────────────────────────┐
│                    柳如烟 桌面客户端 (Tauri v2)              │
│    伙伴单聊 (Companions)     │     多角色圆桌议事厅 (Roundtable) │
└──────────────────────────────┬──────────────────────────────┘
                               │ IPC / WebSocket
┌──────────────────────────────▼──────────────────────────────┐
│                    Python AI Harness 运行引擎                │
├─────────────────────────────────────────────────────────────┤
│ • Session 会话流管理         • 长期记忆图谱与情感反思         │
│ • 递归 Subagent 任务编排     • 多端 IM 网关 (微信/飞书/钉钉)  │
├─────────────────────────────────────────────────────────────┤
│                 四维能力定制工坊 (Ability Hub)               │
│  [Skills 技能包]   [MCP 工业协议]   [自定义脚本]   [外部 ACP]│
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 快速上手与本地开发

### 环境要求
- **Node.js**: >= 18.0.0
- **Python**: >= 3.11
- **Rust**: 1.77+ (用于编译 Tauri 桌面端)

### 1. 克隆仓库与配置
```bash
git clone https://github.com/vooccc/liuruyan.git
cd liuruyan

# 复制密钥模板（纯本地读取，不经过任何第三方中转）
cp models.secrets.json.example models.secrets.json
# 编辑 models.secrets.json 填入您的模型 API Key (如 DeepSeek, OpenAI, Anthropic 等)
```

### 2. 启动开发模式
```bash
# 启动后端 Python Harness
cd backend
python -m venv venv
venv\Scripts\activate  # Windows
pip install -r requirements.txt
python _entry.py

# 另起终端启动前端
cd frontend
npm install
npm run dev:tauri
```

### 3. 一键打包发布 (Windows)
```cmd
一键打包.bat
```
打包脚本将自动构建精简版后端 sidecar 与 Tauri 安装包，产物将生成在安装包目录中。

---

## 🤝 贡献与社区

欢迎提交 Issue 和 Pull Request！参与贡献前请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 📄 开源许可证

本项目基于 [GNU Lesser General Public License v3.0 (LGPL-3.0)](LICENSE) 开源。
- **代码修改与二次分发**：任何对本项目代码的修改并向第三方分发的行为，必须保持以 LGPL-3.0 协议开源全部修改；
- **扩展与插件友好**：允许第三方通过标准 MCP 协议、Skills 扩展包或外部 API 对接本项目，外部独立扩展组件无需强制开源。
