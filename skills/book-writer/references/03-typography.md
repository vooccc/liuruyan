# 中文排版系统

> 执行排版和构建任务时读取此文件。所有规则是中文排版标准，不得以"优化"为由违反。

---

## 字族策略

| 语义角色 | 字族 | CSS 变量 | 场景 |
|----------|------|----------|------|
| 标题/强调 | 黑体 | `--fn-hei` | h1-h4、表头、CTA |
| 正文/阅读 | 宋体 | `--fn-song` | 段落、列表、表格 |
| 引用/辅助 | 楷体 | `--fn-kai` | 引用、题引、金句、过渡语 |

```css
--fn-hei: "Source Han Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", "SimHei", sans-serif;
--fn-song: "Source Han Serif SC", "Noto Serif CJK SC", "SimSun", "宋体", serif;
--fn-kai: "KaiTi", "STKaiti", "楷体", serif;
```

---

## 核心排版规则（严禁违反）

| 规则 | CSS | 说明 |
|------|-----|------|
| 所有段落 2em 缩进 | `p { text-indent: 2em; }` | **无例外**。严禁添加 `h1+p{text-indent:0}` |
| 段落两端对齐 | `p { text-align: justify; }` | |
| h1 左对齐 | `h1 { text-align: left; }` | **不居中**（英文排版惯例，中文不用） |
| h1 无字间距 | `h1 { letter-spacing: 0; }` | 不加 `letter-spacing` |
| h1 仅下边框 | `border-bottom: 3px solid var(--c-main);` | 不加上边框 |
| h1 自动分页 | `h1 { page-break-before: always; }` | |
| h2 左侧色条 | `border-left: 4px solid var(--c-main);` | |
| 金句左对齐 | `text-align: justify; text-indent: 2em;` | **不居中** |
| 金句左金色边条 | `border-left: 3px solid var(--c-gold);` | 不用 `border-image` 装饰线 |
| 题引两端对齐 | `text-align: justify; text-indent: 2em;` | **不居中**、**不斜体** |
| A4 页面 | `@page { size: A4; margin: 25mm 20mm; }` | |

---

## 中英排版差异（最高优先级陷阱）

AI 最容易犯的错误：将英文排版惯例套用到中文。

| 维度 | 英文惯例 | 中文标准 | 常犯错误 |
|------|----------|----------|---------|
| 段落缩进 | 选择性（标题后首段不缩进） | **全部 2em** | 添加 `h1+p{text-indent:0}` |
| 章标题 | 居中 + 大字间距 | **左对齐 + 0 字间距** | `text-align:center; letter-spacing:6px` |
| 金句 | 居中 + 装饰线（pull-quote） | **左对齐 + 左侧边条** | `text-align:center; border-image:gradient` |
| 题引 | 居中、斜体 | **两端对齐、楷体** | `text-align:center; font-style:italic` |

**根因**：搜索"book typography best practices"得到的规则默认面向英文。目标语言是中文，必须主动拒绝英文默认值。

---

## 语义元素 CSS

```css
/* 金句 */
p.jinqu {
  font-family: var(--fn-kai); font-size: 11pt; color: var(--c-main);
  text-align: justify; text-indent: 2em;
  border-left: 3px solid var(--c-gold);
  background: var(--c-accentbg); border-radius: 0 4px 4px 0;
}

/* 题引 */
p.epigraph {
  font-family: var(--fn-kai); font-size: 11pt; color: #666;
  text-align: justify; text-indent: 2em;
  border-bottom: 1px solid rgba(0,0,0,.1);
}

/* 章尾过渡 */
p.chapter-next {
  font-family: var(--fn-kai); font-size: 9.5pt; color: #999;
  text-align: right; text-indent: 0;
}

/* CTA */
.cta-box {
  background: linear-gradient(135deg, var(--c-accentbg), var(--c-lightbg));
  border-left: 5px solid var(--c-gold); border-radius: 6px;
}

/* 流程块 */
.flow-block {
  background: var(--c-accentbg); border-left: 4px solid var(--c-main);
  font-family: var(--fn-hei); white-space: pre-wrap;
}

/* 自检清单 */
ul.checklist { list-style: none; }
ul.checklist li::before {
  content: ''; display: inline-block;
  width: 14px; height: 14px;
  border: 2px solid var(--c-main); border-radius: 3px;
  margin-right: 8px; vertical-align: middle;
}
```

---

## 配色主题化

所有颜色通过 CSS 变量注入，一套 CSS 适配多本书：

```css
:root {
  --c-main: ${book.color};
  --c-gold: ${book.accentGold || '#D4A843'};
  --c-lightbg: ${book.lightBg};
  --c-accentbg: ${book.accentBg};
}
```

配色参考：

| 风格 | 主色 | 适用 |
|------|------|------|
| 琥珀橙 `#E67E22` | 温暖行动感 | 创业/入门 |
| 深青蓝 `#1A5276` | 专业可信 | 顾问/方法论 |
| 橄榄绿 `#27AE60` | 成长组织 | 运营/管理 |
| 薰衣草紫 `#8E44AD` | 新锐个人 | 职场/品牌 |
| 钢青蓝 `#2C5F7C` | 技术沉稳 | 技术手册 |

---

## 语义后处理引擎

MarkdownIt 不区分语义，用 7 条正则规则将标准 HTML 转为带语义 class 的 HTML。

### 七条规则（H1→H7 严格串行）

| 规则 | 检测条件 | 转换结果 |
|------|----------|----------|
| H1 金句 | `<p><strong>纯文字</strong></p>`，不含冒号 | `.jinqu` |
| H2 题引 | h1 紧接的第一个 `<p>` | `.epigraph` |
| H3 过渡语 | `<em>` 以"下一章"或"翻到"开头 | `.chapter-next` |
| H4 流程块 | `<pre><code>` 含 ↓├└→ 符号 | `.flow-block` |
| H5 CTA | `<blockquote>` 含"公众号"或"后台回复" | `.cta-box` |
| H6 图表 | HTML 注释 `<!-- P02-1：标题 -->` + SVG | `.figure` + 图注 |
| H7 清单 | `<li>[ ]` 标记 | `.checklist` |

### Markdown 标记约定

| 语义 | Markdown 写法 | 视觉效果 |
|------|---------------|----------|
| 章标题 | `# 标题` | 24pt 黑体、左对齐、下边框、分页 |
| 节标题 | `## 标题` | 17pt 黑体、左侧色条 |
| 金句 | `**一句话。**`（独立段落，无冒号） | 楷体、金色左边、浅背景 |
| 题引 | `# 标题` 后紧接的第一段 | 楷体、灰色 |
| 过渡语 | `*下一章...*` | 右对齐、小字 |
| 流程图 | 代码块 + ↓├└→ | 黑体、预格式 |
| CTA | `> 公众号...` | 金色边框提示框 |
| 图表 | `<!-- P02-1：标题 -->` + SVG | 居中、编号图注 |
| 清单 | `- [ ] 项目` | CSS checkbox |

---

## 排版检查清单

- [ ] 所有段落首行 2em 缩进（无例外）
- [ ] h1 左对齐、下边框、无字间距
- [ ] 金句：左对齐 + 左金色边条 + 浅色背景
- [ ] 所有内部数据已脱敏
- [ ] SVG 图表中的文字同步检查
- [ ] 流程图使用 ↓├└→ 箭头符号
- [ ] CTA 引用块包含触发关键词
- [ ] 每段不超过 200 字
- [ ] 构建前关闭 PDF 阅读器
- [ ] 三格式（HTML/PDF/DOCX）均正常生成
