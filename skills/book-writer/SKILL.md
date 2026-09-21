---
name: book-writer
description: "人机协同写书（手册/白皮书），含去AI味规则与排版构建"
description_en: "Co-write books & handbooks with AI, anti-AI-flavor rules & typesetting"
---

# Book Writer — 人机协同写书

## 概述

To produce professional Chinese books and handbooks through human-AI collaboration. This skill covers the full pipeline: positioning alignment → outline design → chapter-by-chapter writing → typesetting build → quality review.

**Core constraint**: Human defines direction, AI expands and executes, human confirms. AI never assumes strategic direction, target readers, or content boundaries without confirmation.

---

## 工作流（五阶段 SOP）

### 阶段 1：定位对齐

To start a new book project, gather these four inputs from the user:

1. **核心主张** — 读者合上书时记住的那一句话
2. **目标读者** — 具体描述（职位/行业/痛点），不是泛指
3. **差异点** — 与市面同类书的核心区别
4. **体量** — 轻量手册（3-5万字）/ 标准书籍（8-12万字）/ 白皮书（15万+）

To present positioning confirmation, output:
- 读者画像（一段话，含具体人物原型）
- 一句话定位
- 竞品差异表

Do NOT start writing until the user confirms.

---

### 阶段 2：目录设计

To design the outline, follow the three-layer architecture from `references/02-content-templates.md`:

```
认知建立（30-40%）→ 工具方法（20-30%）→ 场景库（30-40%）
完整骨架：前言 → 认知篇(2-3章) → 工具篇(2-4章) → 场景库 → 行动篇 → 结语 → 附录
```

To present the outline, include:
- 每章标题 + 核心问题
- 字数预算
- 对应模板类型（认知章/工具章/场景库/结语）

To confirm outline: get chapter-by-chapter approval before writing any content.

---

### 阶段 3：逐章写作

Before writing any content, load `references/01-writing-rules.md` for anti-AI-flavor rules.

To write each chapter, apply the correct template from `references/02-content-templates.md`:

| 章节类型 | 模板 | 必备要素 |
|----------|------|----------|
| 前言 | 四层递进 | 破冰故事 + 价值概括 + 内容梗概 + AI声明 |
| 认知篇章 | 认知章模板 | 故事/案例 + 对比表 + 好消息/坏消息 + 小结 + 过渡 |
| 工具篇章 | 工具章模板 | 痛点 + 零术语定义 + 场景 + 完整案例 + 实操清单 |
| 场景库 | 场景模板 | 痛点 + 解法 + 步骤 + 效果数据 + 难度评级 |
| 结语 | 结语模板 | 回扣开篇 + 时代意义 + 行动号召 + 总金句 |
| 附录 | 附录模板 | 速查表 + 操作清单 + 术语表 |

**自检要求**：生成 500 字以上内容后，对照 `references/01-writing-rules.md` 末尾的自检表逐项核对，不合格则重写。

---

### 阶段 4：排版构建

To set up the build system in the project directory:

1. 复制 `assets/build.mjs` → 项目根目录
2. 复制 `assets/style.css` → 项目根目录
3. 复制 `assets/books.config.js` → 项目根目录，按实际书目填写配置
4. 创建 `src/` 目录存放各章 MD 文件，`output/` 目录存放构建产物

To install dependencies:
```bash
npm install markdown-it puppeteer html-to-docx
```

To build:
```bash
node build.mjs B1     # 构建指定书籍
node build.mjs        # 构建全部
```

To verify typesetting, load `references/03-typography.md` and check against the排版检查清单.

---

### 阶段 5：质量审查

To complete quality review, check all three checklists:

1. **排版检查清单** — 见 `references/03-typography.md` 末尾
2. **去AI味检查清单** — 见 `references/01-writing-rules.md` 末尾
3. **内容完整性检查清单** — 见 `references/02-content-templates.md` 末尾

---

## 核心执行约束

To maintain quality, enforce these rules at all times:

| 约束 | 说明 |
|------|------|
| **指正即执行** | 用户纠正事实或方向时，直接修改，不反问确认 |
| **共识用原话** | 记录决策时保留用户原始表述，不改写 |
| **产出即解耦** | 生成的文档必须独立成文，不含"如前所述""根据上文"等依赖会话的表述 |
| **不自行假设** | 读者是谁、内容边界、删改方向——未经确认不执行 |
| **去AI味是硬标准** | 输出的文字读起来像"AI写的"即为不合格，必须重写 |
| **中文排版标准** | 一切排版遵循中文惯例，不套用英文惯例（尤其是缩进和对齐） |

---

## 短指令响应

| 用户说 | 执行 |
|--------|------|
| "写" / "成文" / "落稿" | 根据会话共识生成文档，存入 `src/` 目录 |
| "同步状态" | 将共识同步到 `status.md` |
| "构建" | 执行 `node build.mjs` |
| "自检" | 对照三份检查清单逐项核对当前内容 |

---

## 资源索引

| 资源 | 路径 | 何时读取 |
|------|------|---------|
| 去AI味9条规则 + 自检表 | `references/01-writing-rules.md` | **每次生成文字前** |
| 全书架构 + 各章模板 + 写作要素 | `references/02-content-templates.md` | 规划目录和写章节时 |
| 中文排版规则 + CSS规范 + 后处理引擎 | `references/03-typography.md` | 排版和构建时 |
| 构建系统 + 陷阱教训 | `references/04-build-system.md` | 执行构建时 |
| CSS 排版样式模板 | `assets/style.css` | 搭建项目时复制到工程目录 |
| 构建脚本模板 | `assets/build.mjs` | 搭建项目时复制到工程目录 |
| 书籍配置对象模板 | `assets/books.config.js` | 搭建项目时复制到工程目录 |
