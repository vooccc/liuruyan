# 构建系统与陷阱教训

> 执行构建任务时读取此文件。

---

## 构建系统

### 多书配置（books.config.js）

```javascript
export const BOOKS = [
  {
    id: 'B1',              // node build.mjs B1
    title: '书名',
    subtitle: '副标题',
    color: '#2C5F7C',      // 主色
    lightBg: '#F4F7FA',    // 浅背景
    accentBg: '#E3EDF4',   // 强调背景
    accentGold: '#D4A843', // 金色
    coverImage: '',        // 空则生成 SVG 文字封面
    srcDir: './src/',
    series: 'seriesName',
    author: '作者名',
    files: ['01-引言.md', '02-概念.md', ...],
  },
];
```

### 构建流水线

```
main()
  ├── puppeteer.launch()（共享实例，只启动一次）
  ├── for each book:
  │     ├── buildHTML(book)
  │     │     ├── MarkdownIt.render(合并所有 MD)
  │     │     ├── H1-H7 后处理（严格串行）
  │     │     ├── 提取目录（h1/h2）
  │     │     ├── 生成封面 / 版权页 / 目录页
  │     │     └── 组装完整 HTML
  │     ├── 写入 HTML 预览
  │     ├── Puppeteer → PDF（A4，含页眉页脚）
  │     └── html-to-docx → DOCX
  └── browser.close()
```

### 构建命令

```bash
node build.mjs          # 构建全部
node build.mjs B1       # 构建指定 ID
```

### PDF 关键参数

```javascript
await page.pdf({
  format: 'A4', printBackground: true,
  margin: { top: '25mm', bottom: '25mm', left: '20mm', right: '20mm' },
  displayHeaderFooter: true,
  footerTemplate: '<span style="font-size:9pt;color:#999">第 <span class="pageNumber"></span> 页，共 <span class="totalPages"></span> 页</span>',
});
```

### DOCX 注意事项

- 去掉 Base64 图片减小体积
- `html-to-docx` 对 CSS 支持有限，样式保真度低于 PDF
- 主要用于提供可编辑格式

---

## 陷阱与教训

### PDF 构建问题

| 问题 | 原因 | 解决 |
|------|------|------|
| `EBUSY` 错误 | PDF 在阅读器中打开 | **构建前关闭阅读器** |
| 中文乱码 | 构建环境缺字体 | 安装 `fonts-noto-cjk` |
| SVG 文字缺失 | 系统无对应字体 | 确保 font-family 指定的字体可用 |

### 数据脱敏

| 检查项 | 方法 |
|--------|------|
| 搜索大数字 | `grep -rn "\d+[,.]?\d{3}人\|万人" *.md` |
| SVG 中的文字 | 同步检查图表内的文本节点 |
| 内部配置数据 | 替换为通用描述 |

### Windows 路径处理

- `srcDir` 使用 `path.resolve()` 处理，避免反斜杠问题
- `node build.mjs` 在 PowerShell 下直接运行
- Puppeteer 使用系统字体渲染，Windows 下黑体/宋体/楷体均可用

### 去 AI 味典型失败模式

| 失败模式 | 示例 | 修正 |
|----------|------|------|
| 套话开场 | "在数字化转型浪潮中..." | 用具体人物/场景开场 |
| 概念先行 | "XX 是一种..." | 用问题/困境开场 |
| 排比堆砌 | "首先...其次...最后..." | 改 bullet list 或表格 |
| 万能叙事 | "AI 将彻底改变..." | 加"好消息/坏消息" |
| 注水重述 | "换言之...也就是说..." | 删除重述段 |
| 抽象结尾 | "综上所述..." | 改为具体下一步行动 |
