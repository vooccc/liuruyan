import { describe, expect, it } from 'vitest'
import { renderChatMarkdown, renderUserMessageHtml } from './chatMarkdown'

describe('renderUserMessageHtml', () => {
  it('终端日志中的 === / --- 分隔线不会触发 Setext 大标题且保留全部符号', () => {
    const input = [
      '==================================================',
      'Done in 0s | SSO OK: 0 BUILD OK: 0 FAIL: 1',
      '==================================================',
      '--------------------------------------------------',
    ].join('\n')

    const html = renderUserMessageHtml(input)
    expect(html).not.toMatch(/<h[1-6]\b/i)
    expect(html).toContain('<div class="user-plain-text">')
    expect(html).toContain('==================================================')
    expect(html).toContain('Done in 0s | SSO OK: 0 BUILD OK: 0 FAIL: 1')
    expect(html).toContain('--------------------------------------------------')
  })

  it('# 注释、**加粗**、*斜体*、> 引用、```代码块``` 全按纯文本原样保留', () => {
    const input = [
      '# Python 注释',
      '## 二级注释',
      '**不是加粗** *不是斜体*',
      '> 不是引用',
      '```ts',
      'const x = 1',
      '```',
    ].join('\n')

    const html = renderUserMessageHtml(input)
    expect(html).not.toMatch(/<h[1-6]|<strong|<em|<blockquote|<pre/i)
    expect(html).toContain('# Python 注释')
    expect(html).toContain('## 二级注释')
    expect(html).toContain('**不是加粗** *不是斜体*')
    expect(html).toContain('&gt; 不是引用')
    expect(html).toContain('```ts')
  })

  it('正文中的 HTML 标签原样转义，不作为 HTML 注入', () => {
    const html = renderUserMessageHtml('<script>alert(1)</script>\n<details><summary>fake</summary></details>')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<details>')
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).toContain('&lt;details&gt;&lt;summary&gt;fake&lt;/summary&gt;&lt;/details&gt;')
  })

  it('保留首行缩进与内部空行，只去掉首尾多余空行', () => {
    const html = renderUserMessageHtml('\n\n  indented line\n\nsecond line\n\n')
    expect(html).toBe('<div class="user-plain-text">  indented line\n\nsecond line</div>')
  })

  it('兼容头部注入的 workflow / IDE 上下文 <details> 块，后续用户正文仍为纯文本', () => {
    const input = [
      '<details class="liuruyan-lry-workflow">',
      '<summary>📎 参考工作流：add-new-tool</summary>',
      '',
      '步骤 1',
      '',
      '</details>',
      '',
      '<details class="liuruyan-ide-ctx">',
      '<summary>🪟 IDE 上下文 · foo.ts:1</summary>',
      '',
      '`src/foo.ts`',
      '',
      '</details>',
      '',
      '===',
      '# 用户的普通消息',
    ].join('\n')

    const html = renderUserMessageHtml(input)
    expect(html).toContain('<details class="liuruyan-lry-workflow">')
    expect(html).toContain('<details class="liuruyan-ide-ctx">')
    expect(html).toContain('<div class="user-plain-text">===\n# 用户的普通消息</div>')
    expect(html).not.toMatch(/<h[1-6]\b/i)
  })
})

describe('renderChatMarkdown', () => {
  it('AI 侧 Markdown 标题与加粗渲染不受影响', () => {
    const html = renderChatMarkdown('# 标题\n\n**重点**')
    expect(html).toContain('<h1>标题</h1>')
    expect(html).toContain('<strong>重点</strong>')
  })
})
