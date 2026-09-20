# 歸藏的材质插画 skill

![GitHub stars](https://img.shields.io/github/stars/op7418/guizang-material-illustration?style=flat-square)
![Skill](https://img.shields.io/badge/Skill-Agent-111111?style=flat-square)
![Material Illustration](https://img.shields.io/badge/Material-Illustration-002FA7?style=flat-square)
![Charts](https://img.shields.io/badge/Chart-Beautify-FF6B35?style=flat-square)
![Codex](https://img.shields.io/badge/Codex-Supported-222222?style=flat-square)
![Claude Code](https://img.shields.io/badge/Claude%20Code-Supported-6B5B95?style=flat-square)

一个适配 Claude Code / Codex 等 Agent 环境的配图 Skill，用来把文章、笔记、图表截图、产品概念、工作汇报、教学材料和人文观点，生成**带中文标签的歸藏材质插画**。

<img width="1600" height="900" alt="歸藏的材质插画 skill 顶部封面" src="https://github.com/user-attachments/assets/fc24c36d-197e-4689-abe8-feb0471ac4e5" />

它解决的是「中间那张图」的问题：社交卡片、PPT、文章和文档里经常需要一张能把意思讲清楚的中心配图，而不是一张漂亮但看不懂的装饰图。

这个 Skill 专注做三件事：

- **解释图**：把抽象概念、流程、机制、系统关系画成带标签的图。
- **图表美化**：从截图或原始数据里抽取语义，重新生成更适合传播的材质化图表。
- **参考辅助出图**：遇到冷门概念、品牌、模型、科学装置、历史物件时，先查参考信息和参考图，再统一转成歸藏材质风格。

> 这是 [guizang-social-card-skill](https://github.com/op7418/guizang-social-card-skill) 的配套项目。Social Card Skill 负责整张卡片的标题、正文、主题色和尺寸；这个 Skill 负责卡片里的中心插画。

## 30 秒开始

```bash
npx skills add https://github.com/op7418/guizang-material-illustration --skill guizang-material-illustration
```

也可以直接把这段话发给有 shell 权限的 AI Agent：

```text
帮我安装 guizang-material-illustration。请把 https://github.com/op7418/guizang-material-illustration 克隆到 ~/.claude/skills/guizang-material-illustration，安装完成后检查 SKILL.md、assets/、references/ 是否存在。
```

已经安装过的话，用这段话更新：

```text
帮我更新 guizang-material-illustration。请进入 ~/.claude/skills/guizang-material-illustration 执行 git pull，然后告诉我当前最新 commit。
```

安装后直接对 Agent 说：

```text
用歸藏的材质插画 skill，帮我把这段产品说明做成一张带中文标签的机制图。
```

也可以试这些请求：

```text
把这篇文章挑 3 个核心概念，各生成一张带字配图。
帮我把这张柱状图重新画成歸藏材质风格，数据和坐标不要改。
这段讲 PKCE 的说明太抽象了，先查一下参考信息，再做一张流程图。
给这篇小学科学课文做一张杠杆原理图，图里标出支点、用力点、阻力点和力臂。
把这个周报整理成一张项目状态配图，包含进展、风险、决策、下周。
先生成中心配图，再交给社交媒体卡片 Skill 排成 3:4 小红书卡片。
```

## 效果

- **图内可以有字**：解释图需要短标签、箭头、图例和数据标注时，直接生成在图片里，不把图片降级成无字装饰。
- **材质化 3D 图解**：克制的 Swiss editorial 构图、柔和 3D 材质、清楚的空间关系和少量高亮色。
- **图表语义重画**：输入是糟糕截图时，只保留图表类型、标题、数据、坐标、单位、误差线和结论，不复刻原图排版。
- **参考搜索辅助**：模型 Logo、技术术语、历史文化物件、科学装置、管理框架等内容，先补事实和视觉线索，再统一风格。
- **教育与人文都能接**：小学科学、中学物理、生物化学机制、历史路线、文学意象、社会学概念都可以做成解释图。
- **适配外层排版**：生成的图片可以放进小红书 3:4 卡片、公众号封面、PPT、文档、知识库和文章配图。
- **主题色可扩展**：默认 IKB 蓝，也支持柠檬黄、柠檬绿、安全橙、石墨黑等主题方向。
- **QA 优先**：交付前检查中文标签、数据、裁切、参考准确性和社交卡片尺寸下的可读性。

## 适合 / 不适合

**合适**：文章配图 / 知识解释图 / 产品机制图 / 工作汇报配图 / 数据图表美化 / 教学材料配图 / 人文观点配图 / 社交卡片中心图 / PPT 中心插画 / 冷门概念视觉解释

**不合适**：完整小红书卡片排版（用 Social Card Skill）/ 完整 PPT 结构设计（用 PPT Skill）/ 真实摄影修图 / 人像写真 / 长文海报排版 / 需要严格出版级数据制图的科研图

## 常见使用场景

| 任务 | 推荐方式 |
|------|---------|
| 长文章 → 配图 | 先拆出 1-4 个核心概念，每个概念生成一张解释图 |
| 产品 / 技术说明 | 先查参考信息，再做流程图、层级图、系统关系图 |
| 图表截图美化 | 抽取数据和坐标语义，重新生成材质化图表 |
| 工作汇报 | 用进展、风险、决策、下一步做四象限或流程配图 |
| 内容生产 | 把选题、素材、草稿、发布、复盘画成工作流 |
| 教育解释 | 明确部件、方向、关系和短标签，避免只画氛围 |
| 人文观点 | 用意象 + 结构 + 少量标签，不伪造真实历史现场 |
| 社交卡片联动 | 先生成中心图，再让 Social Card Skill 负责标题和版式 |

## 支持类型

| 类型 | 适合内容 |
|------|---------|
| 概念拆解图 | 一个概念由哪些部分组成 |
| 流程图解 | 输入、步骤、判断、输出 |
| 循环机制图 | 增长循环、反馈回路、迭代飞轮 |
| 对比图 | 前后对比、两条路径、两种策略 |
| 层级 / 架构图 | 系统、依赖、组织、模块关系 |
| 场景解释图 | 办公室、课堂、实验台、城市、历史路线 |
| 科学机制图 | 力、电、磁、生态、生物结构、化学反应 |
| 人文意象图 | 诗歌、历史、哲学、社会学隐喻 |
| 材质化图表 | 柱状图、折线图、甘特图、桑基图、热力图、漏斗图、累计流图 |
| 参考辅助图 | 品牌、模型、专业术语、管理框架、科学装置 |

## 图表美化怎么做

输入可以是一张图表截图，也可以是一组数据。Agent 会先抽取：

- 图表类型
- 标题和结论
- 横轴、纵轴、单位和刻度
- 类别顺序
- 数值、百分比、误差线
- 需要强调的最高值、最低值、瓶颈或异常点

然后重新生成一张更适合传播的图：图表可以更小，旁边可以加入小场景、图标、解释标签和视觉重点。目标不是「给原截图换皮」，而是让读者更快看懂。

## 参考搜索怎么用

参考搜索不是找画风，也不是复制外部图片。

它只解决三个问题：

1. 这个东西是什么。
2. 哪些结构、部件、流程或图标不能画错。
3. 观众靠什么稳定视觉线索一眼识别它。

例如 PKCE、Andon、Zettelkasten、Kirkpatrick、Panopticon、某个模型 Logo、某个科学装置或历史物件，都适合先查参考信息，再进入统一的歸藏材质插画风格。

## 安装

### 方式一：一行命令安装

```bash
npx skills add https://github.com/op7418/guizang-material-illustration --skill guizang-material-illustration
```

### 方式二：把下面这段话直接发给 AI

> 帮我安装 `guizang-material-illustration` 这个 Claude Code / Codex skill。请按下面步骤做：
>
> 1. 确保 `~/.claude/skills/` 目录存在，不存在就创建
> 2. 执行 `git clone https://github.com/op7418/guizang-material-illustration.git ~/.claude/skills/guizang-material-illustration`
> 3. 验证：`ls ~/.claude/skills/guizang-material-illustration/` 应该看到 `SKILL.md`、`assets/`、`references/` 三项
> 4. 告诉我装好了，之后我说「做一张带字配图」「图表美化」「生成材质插画」之类的话就会触发这个 skill

### 方式三：手动命令行

```bash
git clone https://github.com/op7418/guizang-material-illustration.git ~/.claude/skills/guizang-material-illustration
```

## 触发方式

装好后，可以这样说：

- 「帮我生成一张配图」
- 「做一张带字解释图」
- 「把这个概念画成图解插画」
- 「把这张图表美化一下」
- 「给这段工作汇报做一张材质风配图」
- 「这个概念比较冷门，先搜参考信息再生成图」
- 「给这篇小学科学课文做一张解释图」
- 「做一张能放进小红书卡片里的中心图」

## 使用流程

Skill 本身会按下面的方式工作：

1. **理解材料**：读文章、截图、数据或说明，找出真正需要被画出来的关系。
2. **内部判断类型**：不让用户硬选模式，自动判断是流程图、机制图、图表、人文场景还是教育解释图。
3. **必要时查参考**：冷门概念、具体品牌、科学装置、历史物件先补参考信息。
4. **压缩文案**：把每张图压成一句说明和 3-5 个短标签。
5. **写生成提示词**：明确标签、数据、比例、安全区、视觉风格和参考线索。
6. **生成图片**：调用 GPT-Image / imagegen 或当前 Agent 可用的图像生成能力。
7. **检查并重生**：中文标签、数据、裁切、图例、参考线索错了，优先重新生成。
8. **交付资产**：保存图片路径和提示词，方便放进社交卡片、PPT 或文档。

详细执行规则见 [`SKILL.md`](./SKILL.md)。视觉风格、图表、参考搜索和 QA 规则在 `references/*.md` 里。

## 目录结构

```text
guizang-material-illustration/
├── SKILL.md                         # Skill 主文件：触发条件、工作流、交付规则
├── README.md                        # 本文件
├── HANDOFF.md                       # 交接文档：事实、结构、测试案例、验证方式
├── PRODUCT.md                       # 产品文档：定位、场景、边界、roadmap
├── agents/
│   └── openai.yaml                  # Codex / OpenAI Skill 展示配置
├── assets/
│   └── prompt-template.md           # 可复用图像提示词模板
└── references/
    ├── visual-style.md              # 歸藏材质插画风格、比例、安全区、主题色
    ├── prompt-patterns.md           # 循环、流程、Hub、对比、层级等提示结构
    ├── chart-beautify.md            # 图表语义抽取、数据优先重画、图标参考
    ├── use-cases-and-routing.md     # 支持场景与内部路由
    ├── reference-gathering.md       # 生僻概念 / 品牌 / 科学装置参考规则
    └── qa-checklist.md              # 图内文字、数据、裁切、参考准确性检查
```

## 核心设计原则

1. **图要讲人话**：图内标签短、具体、能指向对象，不用抽象名词堆砌。
2. **图内可以有字**：解释图不是纯装饰，必要标签应该直接生成在图里。
3. **数据不能编**：图表类先保证数值、坐标和单位正确，再谈风格。
4. **参考只补事实**：查参考是为了画对，不是为了抄风格。
5. **不让用户选内部模式**：Agent 自己判断类型，只有关键信息缺失时才问。
6. **中心图和外层排版分工**：这个 Skill 生成配图，社交卡片 / PPT Skill 负责整页排版。
7. **小图也要读得清**：最终会进社交卡片时，标签和主体必须能在缩放后看懂。

## 和 Social Card Skill 怎么配合

推荐链路：

1. 用 `guizang-material-illustration` 先生成中心图。
2. 检查图内标签、数据和裁切。
3. 把图片交给 `guizang-social-card-skill`。
4. Social Card Skill 负责 3:4 / 1:1 / 21:9 的标题、正文、主题色和导出。

如果一张卡片的重点是这张图，外层卡片要给图片足够大的区域；不要把中心图缩得太小，否则图内标签会读不清。

## Roadmap

- 整理一组可浏览案例 gallery，覆盖工作、教育、人文、图表四类。
- 扩展图表类型：雷达图、矩阵图、泳道图、时间轴、组织结构图、地图型数据。
- 补参考搜索记录模板：查了什么、提取了什么、哪些线索不采用。
- 增加和 Social Card Skill 联动的最小 recipe。
- 补更多中文图内标签的稳定提示模板。

## FAQ

**这个 Skill 会直接排完整小红书卡片吗？**  
不会。它只生成中心配图。整张卡片的标题、正文、主题色和平台尺寸交给 Social Card Skill。

**图里真的可以有中文字吗？**  
可以，而且这是这个 Skill 的核心。只要图片承担解释任务，短中文标签、箭头说明、数据标注就应该在图里。

**如果中文字生成错了怎么办？**  
优先缩短标签并重新生成，不建议靠 HTML 往图上贴一堆字补救。

**能不能只给数据，不给原图？**  
可以。只要提供图表类型、类别、数值、单位和想强调的结论，就能生成材质化图表。

**能不能查参考图？**  
可以。冷门概念、具体模型、品牌、科学装置、历史文化物件都适合先查参考。参考只用于理解事实和视觉线索。

**支持英文图吗？**  
支持，但默认优先中文图内标签，因为这个项目主要服务中文内容生产。

## 贡献

欢迎开 Issue 或 PR。比较有价值的改动包括：

- 补充新的图表类型和提示模板。
- 补充教育、人文、工作场景案例。
- 改进 `references/reference-gathering.md` 的参考搜索边界。
- 改进 `references/qa-checklist.md` 的图内文字和数据检查规则。
- 给 `assets/prompt-template.md` 增加更稳定的中文标签写法。

测试和 demo 请放在 `local-tests/` 下，不要把一次性输出放进 Skill 根目录。
