# 柳如烟 (Ruyan AI)

> **有灵魂、有记忆、能干活的私人 AI 团队，跑在你自己的电脑上。**

柳如烟是一个本地优先的多角色 AI 桌面应用。你可以创造多位性格鲜明的数字生命，与她们日常对话、共同协作。她们拥有长期记忆，会记住你们的每一次相处；在你授权后，也能真正动手替你操作电脑、拆解并完成任务。所有数据保存在本地，数据主权 100% 属于你。

<!-- 徽章行：静态徽章（私有仓库也能正常显示），不需要可整行删除。
     仓库公开且发布 Release 后，可换回动态徽章：
     [![Release](https://img.shields.io/github/v/release/vooccc/liuruyan?label=%E6%9C%80%E6%96%B0%E7%89%88%E6%9C%AC)](https://github.com/vooccc/liuruyan/releases/latest)
     [![Stars](https://img.shields.io/github/stars/vooccc/liuruyan?style=flat&label=Stars)](https://github.com/vooccc/liuruyan/stargazers) -->
![Desktop](https://img.shields.io/badge/Desktop-Tauri_v2-blue?logo=tauri)
![Frontend](https://img.shields.io/badge/Frontend-React_18_%7C_TypeScript-61dafb?logo=react)
![Backend](https://img.shields.io/badge/Backend-Python_Harness-yellow?logo=python)
![Protocol](https://img.shields.io/badge/Protocol-MCP_%7C_Skills_%7C_ACP-purple)
![Platform](https://img.shields.io/badge/Platform-Windows_10_%7C_11-lightgrey?logo=windows)
[![Release](https://img.shields.io/badge/Release-Download-brightgreen?logo=github)](https://github.com/vooccc/liuruyan/releases/latest)
![License](https://img.shields.io/badge/License-Apache_2.0-blue)

---

## 目录

- [柳如烟适合谁](#柳如烟适合谁)
- [核心特性](#核心特性)
- [界面一览](#界面一览)
- [快速开始](#快速开始)
- [常见问题](#常见问题)
- [友情链接](#友情链接)
- [参与贡献](#参与贡献)
- [开源许可证与角色资产保护声明](#开源许可证与角色资产保护声明)

---

## 柳如烟适合谁

- 想要一位有灵魂有温度又会干活又能成长的 AI 搭子，希望她记得每一次相处的用户
- 喜要尝试多个 AI 角色围坐讨论、观点碰撞、帮你一起出主意或者干活的人
- 开发者与效率玩家：让 AI 接上工具，真正替你查资料、写代码、操作电脑
- 在意隐私，希望对话、记忆、角色全部保存在自己电脑里的人

## 核心特性

### 🎭 有灵魂的角色

每个角色由「性格基因 + 动态心智」构成，而不是一段简单的人设 prompt。内置多位风格各异的角色模板，也支持完全自定义。

- 性格基因（GENOME）决定角色底色，动态心智随你们的相处不断演化
- 角色会自主撰写情绪反思日记，记录关系的变化
- 头像、语气、称呼、关系边界都可以按你的喜好调整

### 🧠 记得你的长期记忆

- 聊天中的重要内容会自动沉淀为长期记忆、日记与反思，跨会话持续生效
- 全部以本地 Markdown / JSON 存储，随时可以查看、修改、备份
- 社交图谱会记录角色之间、角色与你之间的关系变化与重要事件

### 👥 多角色协作与圆桌

- 让多个角色同席讨论一个问题：观点碰撞、互相补充，你随时可以插话
- 支持协作聊天与专家咨询（Consult）两种协作形态
- 复杂任务交给递归 Subagent 子代理在后台完成，结果自动回注对话

### 🛠 真正能干活

- **四维能力工坊**：为每个角色按需插拔 Skills 技能包、MCP 服务、内置 Python 工具与外部 ACP 智能体，避免全量注入
- **计算机使用**：授权后角色可以直接操作你的桌面应用，完成真实操作
- **任务进度**：复杂任务自动拆解为任务清单，在对话流里实时查看进度
- **IDE 联动**：官方 VSCode  / Cursor / Cursor 插件，支持文件跳转、内联 Diff、诊断互通
- **多端触达**：微信 / 飞书 / 钉钉 / Matrix 机器人，出门在外也能远程派活、接收结果
- **语音**：内置 TTS 语音合成与语音输入

### 🏠 本地优先，数据主权归你

- 基于 Tauri v2，安装包小、启动快、内存占用低
- 对话、记忆、角色配置保存在本地清晰命名的 Markdown / JSON 目录中
- 零遥测、零云端中转；模型 API Key 只在本机使用，请求由你的电脑直接发往模型服务商

<!-- 加截图：在 GitHub 网页上编辑本文件时，直接把图片拖进正文即可自动上传并插入链接；图片顺序 = 它们在文中的先后位置。也可把 png 放进 docs/assets/ 后写 ![说明](docs/assets/xxx.png) -->

## 界面一览

### 主聊天 —— 思考链、工具调用、任务进度实时可见
<img width="1430" height="1018" alt="image" src="https://github.com/user-attachments/assets/102c04aa-d761-4a0a-bb4c-d93325611a41" />

### 圆桌协作 —— 多角色同席讨论、观点碰撞
<img width="1375" height="1018" alt="image" src="https://github.com/user-attachments/assets/ff712583-f04a-40d4-bb34-18afb6474a79" />

### 角色工坊 —— 自定义角色的心魂与能力装备
<img width="1375" height="1018" alt="image" src="https://github.com/user-attachments/assets/d7498c61-5957-4674-ad03-6676196d70c7" />

### 任务进度 —— 复杂任务自动拆解、实时跟踪
<img width="1430" height="1018" alt="a3e51a7b-6209-46c4-a41e-a3a3e18701e0" src="https://github.com/user-attachments/assets/fd586e90-6c49-4a02-8d56-adcc678f47d6" />


## 快速开始

### 方式一：下载安装包（推荐）

1. 前往 [Releases 页面](https://github.com/vooccc/liuruyan/releases/latest) 下载 Windows 安装包（`.exe`）或绿色免安装版（`.zip`）
2. 启动后，在设置中填入你的模型 API Key（OpenAI / DeepSeek / Anthropic / 本地 Ollama 等 OpenAI 兼容接口均可）
3. 开始对话

### 方式二：从源码运行

环境要求：Node.js ≥ 18、Python ≥ 3.11、Rust 1.77+（用于编译 Tauri 桌面端）

```bash
git clone https://github.com/vooccc/liuruyan.git
cd liuruyan

# 启动后端
cd backend
python -m venv venv
venv\Scripts\activate          # Windows；macOS / Linux 使用 source venv/bin/activate
pip install -r requirements.txt
python _entry.py

# 另开一个终端，启动前端
cd frontend
npm install
npm run dev:tauri
```

## 常见问题

**需要付费吗？**
柳如烟本身免费开源。模型调用使用你自己的 API Key，费用取决于你选择的模型服务商。

**我的数据会上传吗？**
不会。所有对话与记忆保存在本地，程序零遥测；模型请求从你的电脑直接发往你配置的服务商。

**不会编程能用吗？**
可以。下载安装包、填一个 API Key 就能开始使用；「从源码运行」仅面向开发者。

**支持 macOS / Linux 吗？**
桌面端基于 Tauri v2，源码可跨平台构建；目前官方安装包优先支持 Windows。

**和网页版 AI 聊天有什么区别？**
角色与记忆保存在本地、长期生效；多个角色可以协作讨论；接上工具后她们能真正操作你的电脑完成任务，而不只是聊天。

## 友情链接

感谢 [LINUX DO](https://linux.do/) 社区支持

## 参与贡献

欢迎提交 Issue 与 Pull Request！在参与贡献前，请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。为了保障开源项目的长期健康与产权清晰，首次提交 PR 时请签署 [CLA 个人贡献者许可协议](CLA.md)（GitHub Bot 会自动在 PR 评论区引导一键签署）。

## 开源许可证与角色资产保护声明

本项目采用 **双轨授权模式**：

1. **核心工程代码**（Tauri 桌面端底座、Worker 调度运行时、能力工坊 Ability Hub、本地记忆与反思系统等）：基于 [Apache License 2.0](LICENSE) 开源。商业友好，允许自由修改、衍生开发与商用，请保留原作者版权与商标声明。
2. **官方角色与人设资产**（`agents/` 目录下由官方提供的角色人设文件、Prompt 架构、原创文案、官方立绘与头像）：采用 [CC BY-NC-ND 4.0（知识共享 署名-非商业性使用-禁止演绎）](https://creativecommons.org/licenses/by-nc-nd/4.0/deed.zh-hans) 保护。允许个人非商业性体验与学习交流，**严禁未经授权用于商业营利、换皮倒卖或打包收费**。

> 💡 **自由拓展**：开发者可自由基于本项目创建属于自己的新角色，并自由决定新角色的授权许可。
