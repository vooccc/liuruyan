# 贡献指南 (Contributing to Ruyan AI / 柳如烟)

感谢你对 **柳如烟 (Ruyan AI)** 的关注与支持！

## 开发原则与行为准则
1. **轻量至上**：桌面端基于 Tauri v2 + Rust，严禁引入未经压缩的大型无用三方依赖。
2. **可插拔架构**：鼓励将新功能封装为独立的 **MCP 服务** 或 **Skills 技能包**，避免向核心运行时硬编码业务逻辑。
3. **安全第一**：严禁在代码、注释或配置文件中提交真实 API Key 与敏感数据。

## 提交 Issue 与 PR
- 报告 Bug 时，请附带复现步骤、操作系统版本与控制台报错信息。
- 提交 PR 前，请确保运行以下校验：
  - 前端构建检查：`cd frontend && npm run build`
  - 代码风格检查：保持 TypeScript 与 Python PEP8 规范。

## 签署贡献者协议 (CLA)
为了保障开源项目的长期健康与代码产权清晰，本项目配置了自动化 CLA Assistant。
首次提交 PR 时，GitHub Bot 会在评论区留言引导。你只需回复：
`I have read the CLA Document and I hereby sign the CLA`
完成一键签署后，PR 校验即可自动通过。详情请参阅 [CLA.md](CLA.md)。
