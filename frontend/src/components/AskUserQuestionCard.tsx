import { useMemo, useState } from 'react'
import { AlertCircle, CheckCircle2, HelpCircle, Loader2, Send } from 'lucide-react'
import { renderChatMarkdown } from './chatMarkdown'

// 与后端 ask_user_question_tool.py 的返回形状对齐
export interface AskUserQuestionOption {
  label: string
  description: string
  preview?: string | null
}

export interface AskUserQuestionItem {
  question: string
  header: string
  options: AskUserQuestionOption[]
  multiSelect: boolean
}

export interface AskUserQuestionPayload {
  status: 'awaiting_user_choice'
  ask_id: string
  questions: AskUserQuestionItem[]
  note?: string
}

export type AskExecutionState = 'draft' | 'submitting' | 'submitted' | 'failed'

export interface AskUserAnswer {
  question: string
  answer: string // 多选时逗号连接
}

interface AskUserQuestionCardProps {
  payload: AskUserQuestionPayload
  toolUseId?: string
  executionState?: AskExecutionState
  executionError?: string | null
  onSubmit?: (askId: string, toolUseId: string | undefined, answers: AskUserAnswer[]) => void
}

const OTHER_SENTINEL = '__other__'

function answerString(
  question: AskUserQuestionItem,
  selected: Set<string>,
  otherText: string,
): string {
  const labels: string[] = []
  for (const opt of question.options) {
    if (selected.has(opt.label)) labels.push(opt.label)
  }
  if (selected.has(OTHER_SENTINEL)) {
    const txt = otherText.trim()
    labels.push(txt ? txt : '(其他)')
  }
  return labels.join(', ')
}

export default function AskUserQuestionCard({
  payload,
  toolUseId,
  executionState = 'draft',
  executionError,
  onSubmit,
}: AskUserQuestionCardProps) {
  const { ask_id, questions } = payload

  // 每个问题独立的选中集合 + Other 自由文本
  const [selectedByQ, setSelectedByQ] = useState<Record<number, Set<string>>>(
    () => Object.fromEntries(questions.map((_, i) => [i, new Set<string>()])),
  )
  const [otherTextByQ, setOtherTextByQ] = useState<Record<number, string>>(() =>
    Object.fromEntries(questions.map((_, i) => [i, ''])),
  )
  // 每题当前聚焦的选项（用于渲染 preview 侧边栏）
  const [focusedByQ, setFocusedByQ] = useState<Record<number, string | null>>(() =>
    Object.fromEntries(questions.map((_, i) => [i, null])),
  )

  const isLocked = executionState === 'submitting' || executionState === 'submitted'

  const toggleOption = (qIdx: number, label: string) => {
    if (isLocked) return
    const q = questions[qIdx]
    setSelectedByQ(prev => {
      const curr = new Set(prev[qIdx] ?? [])
      if (q.multiSelect) {
        if (curr.has(label)) curr.delete(label)
        else curr.add(label)
      } else {
        // 单选：切换成只保留当前
        if (curr.has(label)) curr.clear()
        else {
          curr.clear()
          curr.add(label)
        }
      }
      return { ...prev, [qIdx]: curr }
    })
    setFocusedByQ(prev => ({ ...prev, [qIdx]: label }))
  }

  const setOtherText = (qIdx: number, text: string) => {
    if (isLocked) return
    setOtherTextByQ(prev => ({ ...prev, [qIdx]: text }))
  }

  const allAnswered = useMemo(() => {
    return questions.every((_q, i) => {
      const sel = selectedByQ[i] ?? new Set<string>()
      if (sel.size === 0) return false
      if (sel.has(OTHER_SENTINEL) && !(otherTextByQ[i] ?? '').trim()) return false
      return true
    })
  }, [questions, selectedByQ, otherTextByQ])

  const handleSubmit = () => {
    if (!onSubmit || !allAnswered || isLocked) return
    const answers: AskUserAnswer[] = questions.map((q, i) => ({
      question: q.question,
      answer: answerString(q, selectedByQ[i] ?? new Set(), otherTextByQ[i] ?? ''),
    }))
    onSubmit(ask_id, toolUseId, answers)
  }

  return (
    <div className="ask-user-card">
      <div className="ask-user-head">
        <div className="ask-user-badge">
          <HelpCircle size={14} />
          <span>需要你的选择</span>
        </div>
        <div className={`ask-user-status is-${executionState}`}>
          {executionState === 'submitting' && <Loader2 size={13} className="spin" />}
          {executionState === 'submitted' && <CheckCircle2 size={13} />}
          {executionState === 'failed' && <AlertCircle size={13} />}
          <span>
            {executionState === 'draft' && '待回答'}
            {executionState === 'submitting' && '提交中'}
            {executionState === 'submitted' && '已回答'}
            {executionState === 'failed' && '提交失败'}
          </span>
        </div>
      </div>

      {questions.map((q, qIdx) => {
        const sel = selectedByQ[qIdx] ?? new Set<string>()
        const focused = focusedByQ[qIdx]
        const focusedOpt =
          focused && focused !== OTHER_SENTINEL
            ? q.options.find(o => o.label === focused)
            : undefined
        const showPreviewPanel = Boolean(focusedOpt?.preview)

        return (
          <div key={qIdx} className="ask-user-question">
            <div className="ask-user-question-head">
              <span className="ask-user-chip">{q.header}</span>
              {q.multiSelect && <span className="ask-user-multi-hint">可多选</span>}
            </div>
            <div className="ask-user-question-text">{q.question}</div>

            <div
              className={
                showPreviewPanel
                  ? 'ask-user-options-body has-preview'
                  : 'ask-user-options-body'
              }
            >
              <ul className="ask-user-options">
                {q.options.map(opt => {
                  const isSel = sel.has(opt.label)
                  return (
                    <li
                      key={opt.label}
                      className={`ask-user-option ${isSel ? 'is-selected' : ''} ${
                        isLocked ? 'is-locked' : ''
                      }`}
                      onMouseEnter={() =>
                        setFocusedByQ(prev => ({ ...prev, [qIdx]: opt.label }))
                      }
                    >
                      <button
                        type="button"
                        className="ask-user-option-btn"
                        onClick={() => toggleOption(qIdx, opt.label)}
                        disabled={isLocked}
                      >
                        <span className="ask-user-option-indicator">
                          {q.multiSelect ? (isSel ? '☑' : '☐') : isSel ? '●' : '○'}
                        </span>
                        <span className="ask-user-option-body">
                          <span className="ask-user-option-label">{opt.label}</span>
                          {opt.description && (
                            <span className="ask-user-option-desc">{opt.description}</span>
                          )}
                        </span>
                      </button>
                    </li>
                  )
                })}

                {/* Other 兜底 */}
                <li
                  className={`ask-user-option is-other ${
                    sel.has(OTHER_SENTINEL) ? 'is-selected' : ''
                  } ${isLocked ? 'is-locked' : ''}`}
                  onMouseEnter={() =>
                    setFocusedByQ(prev => ({ ...prev, [qIdx]: OTHER_SENTINEL }))
                  }
                >
                  <button
                    type="button"
                    className="ask-user-option-btn"
                    onClick={() => toggleOption(qIdx, OTHER_SENTINEL)}
                    disabled={isLocked}
                  >
                    <span className="ask-user-option-indicator">
                      {q.multiSelect
                        ? sel.has(OTHER_SENTINEL)
                          ? '☑'
                          : '☐'
                        : sel.has(OTHER_SENTINEL)
                        ? '●'
                        : '○'}
                    </span>
                    <span className="ask-user-option-body">
                      <span className="ask-user-option-label">其他</span>
                      <span className="ask-user-option-desc">自定义文本回答</span>
                    </span>
                  </button>
                  {sel.has(OTHER_SENTINEL) && (
                    <input
                      type="text"
                      className="ask-user-other-input"
                      value={otherTextByQ[qIdx] ?? ''}
                      onChange={e => setOtherText(qIdx, e.target.value)}
                      onClick={e => e.stopPropagation()}
                      placeholder="输入你的回答…"
                      disabled={isLocked}
                    />
                  )}
                </li>
              </ul>

              {showPreviewPanel && focusedOpt?.preview && (
                <div
                  className="ask-user-preview"
                  dangerouslySetInnerHTML={{
                    __html: renderChatMarkdown(focusedOpt.preview),
                  }}
                />
              )}
            </div>
          </div>
        )
      })}

      {executionError && <div className="ask-user-error">{executionError}</div>}

      {executionState !== 'submitted' && (
        <div className="ask-user-actions">
          <button
            type="button"
            className="ask-user-submit-btn"
            onClick={handleSubmit}
            disabled={!allAnswered || isLocked}
          >
            {executionState === 'submitting' ? (
              <Loader2 size={14} className="spin" />
            ) : (
              <Send size={14} />
            )}
            <span>{executionState === 'submitting' ? '提交中…' : '提交回答'}</span>
          </button>
        </div>
      )}
    </div>
  )
}
