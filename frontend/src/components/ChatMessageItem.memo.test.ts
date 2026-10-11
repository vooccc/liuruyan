/**
 * P5：ChatMessageItem memo 比较器决策逻辑测试。
 *
 * 证明 memo 在长会话流式时只重渲染当前流式消息、跳过历史消息：
 * - 只回调身份变（map 每次 render 新建内联回调）→ 跳过（流式的赢点）
 * - message 引用变（当前流式条换新引用）→ 重渲染
 * - 两个展开 Set 引用变（用户 toggle）→ 重渲染
 * - copied / 标量变 → 重渲染
 * - 可选回调存在性翻转 → 重渲染
 */
import { describe, expect, it } from 'vitest'
import { arePropsEqual } from './ChatMessageItem'

const MSG = { id: 1, role: 'assistant', content: 'hi', segments: [] } as any
const THINK = new Set<string>()
const TOOLS = new Set<string>()
const RENDER = (t: string) => t
const ASK = {} as any

function makeProps(over: Record<string, unknown> = {}) {
  return {
    message: MSG,
    sourceAgent: null,
    avatarSrc: null,
    timeLabel: '10:00',
    copied: false,
    onCopy: () => {},
    expandedThinking: THINK,
    onToggleThinking: () => {},
    expandedToolCalls: TOOLS,
    onToggleToolCall: () => {},
    renderMarkdown: RENDER,
    askStatesByAskId: ASK,
    isBusy: false,
    sessionId: 's1',
    ...over,
  } as any
}

describe('ChatMessageItem arePropsEqual', () => {
  it('只回调身份变（每条消息每次 render 新建内联回调）→ 跳过重渲染', () => {
    const prev = makeProps({ onCopy: () => 'a', onToggleThinking: () => 'a', onInplaceEdit: () => 'a', onSendEdit: () => 'a' })
    const next = makeProps({ onCopy: () => 'b', onToggleThinking: () => 'b', onInplaceEdit: () => 'b', onSendEdit: () => 'b' })
    expect(arePropsEqual(prev, next)).toBe(true)
  })

  it('message 引用变（当前流式条更新）→ 重渲染', () => {
    const prev = makeProps()
    const next = makeProps({ message: { ...MSG } })
    expect(arePropsEqual(prev, next)).toBe(false)
  })

  it('expandedThinking 引用变（用户 toggle 思考）→ 重渲染', () => {
    const prev = makeProps()
    const next = makeProps({ expandedThinking: new Set<string>() })
    expect(arePropsEqual(prev, next)).toBe(false)
  })

  it('expandedToolCalls 引用变（用户 toggle 工具卡）→ 重渲染', () => {
    const prev = makeProps()
    const next = makeProps({ expandedToolCalls: new Set<string>() })
    expect(arePropsEqual(prev, next)).toBe(false)
  })

  it('copied 变（复制本条）→ 重渲染', () => {
    expect(arePropsEqual(makeProps(), makeProps({ copied: true }))).toBe(false)
  })

  it('timeLabel 同值（不同字符串实例同内容）→ 跳过', () => {
    const prev = makeProps({ timeLabel: '10:0' + '0' })
    const next = makeProps({ timeLabel: '10:00' })
    expect(arePropsEqual(prev, next)).toBe(true)
  })

  it('可选回调存在性翻转（onRollback undefined→定义）→ 重渲染', () => {
    const prev = makeProps({ onRollback: undefined })
    const next = makeProps({ onRollback: () => {} })
    expect(arePropsEqual(prev, next)).toBe(false)
  })

  it('v6.1 editMark 变（无→target→after）→ 重渲染；onSendEdit 存在性翻转 → 重渲染', () => {
    expect(arePropsEqual(makeProps(), makeProps({ editMark: 'target' }))).toBe(false)
    expect(arePropsEqual(makeProps({ editMark: 'target' }), makeProps({ editMark: 'after' }))).toBe(false)
    expect(arePropsEqual(makeProps(), makeProps({ onSendEdit: () => {} }))).toBe(false)
    // 存在性不变、仅身份变 → 跳过
    expect(arePropsEqual(
      makeProps({ onSendEdit: () => 'a' }),
      makeProps({ onSendEdit: () => 'b' }),
    )).toBe(true)
  })

  it('isBusy / sessionId / sourceAgent / askStatesByAskId 变 → 重渲染', () => {
    expect(arePropsEqual(makeProps(), makeProps({ isBusy: true }))).toBe(false)
    expect(arePropsEqual(makeProps(), makeProps({ sessionId: 's2' }))).toBe(false)
    expect(arePropsEqual(makeProps(), makeProps({ sourceAgent: { id: 'x' } }))).toBe(false)
    expect(arePropsEqual(makeProps(), makeProps({ askStatesByAskId: {} }))).toBe(false)
  })
})
