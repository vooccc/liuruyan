/**
 * build.mjs — 中文书籍构建脚本
 *
 * 用法：
 *   node build.mjs         构建全部书籍
 *   node build.mjs B1      构建指定 ID 的书籍
 *
 * 依赖：
 *   npm install markdown-it puppeteer html-to-docx
 *
 * 输出：
 *   output/book-{id}.html
 *   output/book-{id}.pdf
 *   output/book-{id}.docx
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import MarkdownIt from 'markdown-it';
import puppeteer from 'puppeteer';
import HTMLtoDOCX from 'html-to-docx';
import { BOOKS } from './books.config.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ── MarkdownIt 配置 ───────────────────────────────────────────
const md = new MarkdownIt({
  html: true,       // 允许 HTML（用于图表注释）
  linkify: true,
  typographer: true,
  breaks: false,
});

// ── CSS 读取 ─────────────────────────────────────────────────
const cssTemplate = fs.readFileSync(path.join(__dirname, 'style.css'), 'utf-8');

// ── 语义后处理流水线（H1→H7 严格串行）──────────────────────────
function postProcess(html, book) {
  // H1：金句 — <p><strong>纯文字（无冒号）</strong></p> → .jinqu
  html = html.replace(
    /<p><strong>([^<:：]+?)<\/strong><\/p>/g,
    '<p class="jinqu"><strong>$1</strong></p>'
  );

  // H2：题引 — h1 紧接的第一个 <p>（非 .jinqu）→ .epigraph
  html = html.replace(
    /(<h1[^>]*>.*?<\/h1>\s*)(<p(?! class="jinqu"))/g,
    '$1<p class="epigraph"'
  );

  // H3：过渡语 — <em> 以"下一章"或"翻到"开头 → .chapter-next
  html = html.replace(
    /<p><em>(下一章|翻到)(.*?)<\/em><\/p>/g,
    '<p class="chapter-next"><em>$1$2</em></p>'
  );

  // H4：流程块 — <pre><code> 含 ↓├└→ 等符号 → .flow-block
  html = html.replace(
    /<pre><code>([\s\S]*?[↓├└→─│]([\s\S]*?))<\/code><\/pre>/g,
    '<pre class="flow-block">$1$2</pre>'
  );

  // H5：CTA — <blockquote> 含"公众号"或"后台回复" → .cta-box
  html = html.replace(
    /<blockquote>([\s\S]*?(?:公众号|后台回复)[\s\S]*?)<\/blockquote>/g,
    '<div class="cta-box">$1</div>'
  );

  // H6：图表 — <!-- P\d\d-\d：标题 --> + SVG → .figure + 图注
  html = html.replace(
    /<!-- (P\d{2}-\d+)：([^-]+) -->\s*(<svg[\s\S]*?<\/svg>)/g,
    '<div class="figure">$3<p class="figure-caption">图 $1：$2</p></div>'
  );

  // H7：清单 — <li>[ ] → ul.checklist
  html = html.replace(
    /<ul>\s*((?:<li>\[ \][\s\S]*?<\/li>\s*)+)<\/ul>/g,
    '<ul class="checklist">$1</ul>'
  );
  html = html.replace(/<li>\[ \] /g, '<li>');

  return html;
}

// ── 提取目录（h1/h2）────────────────────────────────────────
function extractTOC(html) {
  const toc = [];
  const re = /<(h[12])[^>]*>(.*?)<\/\1>/gi;
  let m;
  while ((m = re.exec(html)) !== null) {
    toc.push({ level: m[1], text: m[2].replace(/<[^>]+>/g, '') });
  }
  return toc;
}

// ── 生成 SVG 文字封面 ─────────────────────────────────────────
function generateCoverSVG(book) {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 595 842" width="595" height="842">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="60%" y2="100%">
      <stop offset="0%" style="stop-color:${book.color};stop-opacity:1" />
      <stop offset="100%" style="stop-color:#0a2d4a;stop-opacity:1" />
    </linearGradient>
  </defs>
  <rect width="595" height="842" fill="url(#bg)"/>
  <rect x="0" y="600" width="595" height="242" fill="rgba(0,0,0,0.25)"/>
  <text x="48" y="340" font-family="Microsoft YaHei, SimHei, sans-serif" font-size="40" font-weight="bold" fill="white">${book.title}</text>
  <text x="48" y="390" font-family="KaiTi, STKaiti, serif" font-size="20" fill="rgba(255,255,255,0.8)">${book.subtitle || ''}</text>
  <text x="48" y="780" font-family="Microsoft YaHei, SimHei, sans-serif" font-size="16" fill="rgba(255,255,255,0.7)">${book.author || ''}</text>
  ${book.series ? `<text x="48" y="810" font-family="Microsoft YaHei, SimHei, sans-serif" font-size="12" fill="rgba(255,255,255,0.5)">${book.series}</text>` : ''}
</svg>`;
}

// ── 生成目录 HTML ─────────────────────────────────────────────
function generateTOCHTML(toc) {
  let html = '<div class="toc-page"><div class="toc-title">目录</div>';
  for (const item of toc) {
    if (item.level === 'h1') {
      html += `<div class="toc-chapter"><span>${item.text}</span><span class="toc-dots"></span><span>—</span></div>`;
    } else {
      html += `<div class="toc-section"><span>${item.text}</span><span class="toc-dots"></span><span>—</span></div>`;
    }
  }
  html += '</div>';
  return html;
}

// ── 构建单本书的 HTML ─────────────────────────────────────────
function buildHTML(book) {
  // 合并所有 MD 文件
  const srcDir = path.resolve(__dirname, book.srcDir);
  const combined = book.files
    .map(f => {
      const fp = path.join(srcDir, f);
      if (!fs.existsSync(fp)) {
        console.warn(`⚠ 文件不存在，跳过：${fp}`);
        return '';
      }
      return fs.readFileSync(fp, 'utf-8');
    })
    .join('\n\n');

  // MarkdownIt 渲染
  let contentHTML = md.render(combined);

  // H1-H7 后处理
  contentHTML = postProcess(contentHTML, book);

  // 提取目录
  const toc = extractTOC(contentHTML);

  // 封面
  const coverHTML = book.coverImage
    ? `<div class="cover-page"><img src="${book.coverImage}" style="width:100%;height:100%;object-fit:cover"/></div>`
    : `<div class="cover-page">${generateCoverSVG(book)}</div>`;

  // 版权页
  const copyrightHTML = `<div class="copyright-page">
    <p>${book.title}${book.subtitle ? '：' + book.subtitle : ''}</p>
    <p>作者：${book.author || '—'}</p>
    <p>本书在资料整理、数据分析和初稿生成过程中使用了 AI 辅助工具。<br>
       所有内容均经过审核、事实核查和独立判断。最终文责由作者承担。</p>
    <p>版本：${new Date().toISOString().slice(0, 10)}</p>
  </div>`;

  // 目录页
  const tocHTML = generateTOCHTML(toc);

  // 注入 CSS 变量
  const css = cssTemplate
    .replace('/* 主色，由 book.color 替换 */', book.color + ';')
    .replace('/* 金色，由 book.accentGold 替换，默认 #D4A843 */', (book.accentGold || '#D4A843') + ';')
    .replace('/* 浅背景，由 book.lightBg 替换 */', (book.lightBg || '#F4F7FA') + ';')
    .replace('/* 强调背景，由 book.accentBg 替换 */', (book.accentBg || '#E3EDF4') + ';');

  // 组装完整 HTML
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>${book.title}${book.subtitle ? ' — ' + book.subtitle : ''}</title>
  <style>${css}</style>
</head>
<body>
  ${coverHTML}
  ${copyrightHTML}
  ${tocHTML}
  <div class="main-content">
    ${contentHTML}
  </div>
</body>
</html>`;
}

// ── 主流程 ────────────────────────────────────────────────────
async function main() {
  // 筛选要构建的书
  const targetId = process.argv[2];
  const books = targetId
    ? BOOKS.filter(b => b.id === targetId)
    : BOOKS;

  if (books.length === 0) {
    console.error(`❌ 找不到 id 为 "${targetId}" 的书籍配置`);
    process.exit(1);
  }

  // 确保 output 目录存在
  const outputDir = path.join(__dirname, 'output');
  if (!fs.existsSync(outputDir)) fs.mkdirSync(outputDir, { recursive: true });

  // 启动 Puppeteer（共享实例）
  const browser = await puppeteer.launch({
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--font-render-hinting=none'],
  });

  for (const book of books) {
    console.log(`\n📖 构建：${book.title} (${book.id})`);

    try {
      // 生成 HTML
      const html = buildHTML(book);
      const htmlPath = path.join(outputDir, `book-${book.id}.html`);
      fs.writeFileSync(htmlPath, html, 'utf-8');
      console.log(`  ✅ HTML → ${htmlPath}`);

      // 生成 PDF
      const pdfPath = path.join(outputDir, `book-${book.id}.pdf`);
      const page = await browser.newPage();
      await page.setContent(html, { waitUntil: 'networkidle0' });
      await page.pdf({
        path: pdfPath,
        format: 'A4',
        printBackground: true,
        margin: { top: '25mm', bottom: '25mm', left: '20mm', right: '20mm' },
        displayHeaderFooter: true,
        headerTemplate: `<div style="font-size:9pt;color:#999;width:100%;padding:0 20mm;display:flex;justify-content:space-between;">
          <span>${book.title}</span><span class="title"></span></div>`,
        footerTemplate: `<div style="font-size:9pt;color:#999;width:100%;text-align:center;">
          第 <span class="pageNumber"></span> 页，共 <span class="totalPages"></span> 页</div>`,
      });
      await page.close();
      console.log(`  ✅ PDF  → ${pdfPath}`);

      // 生成 DOCX
      // 去掉 Base64 图片减小体积
      const htmlForDocx = html.replace(/src="data:image\/[^"]+"/g, 'src=""');
      const docxBuffer = await HTMLtoDOCX(htmlForDocx, null, {
        table: { row: { cantSplit: true } },
        footer: true,
        pageNumber: true,
      });
      const docxPath = path.join(outputDir, `book-${book.id}.docx`);
      fs.writeFileSync(docxPath, docxBuffer);
      console.log(`  ✅ DOCX → ${docxPath}`);

    } catch (err) {
      console.error(`  ❌ 构建失败：${err.message}`);
    }
  }

  await browser.close();
  console.log('\n🎉 构建完成！');
}

main().catch(err => {
  console.error('❌ 构建异常：', err);
  process.exit(1);
});
