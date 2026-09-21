/**
 * TransportDropdown —— 自绘下拉选择器（替代 native select）。
 *
 * 为什么自绘：native <select> 的 option popup 是 OS 原生 UI，
 * padding/字体/hover 各家不一致，跟产品视觉断裂；现代成熟产品
 * （Vercel / Linear / Notion / Cherry Studio）这种场景全是自绘 popover。
 *
 * 保留 a11y：
 *   - role="combobox" + aria-expanded / aria-controls
 *   - listbox role + option role + aria-selected
 *   - 键盘：ArrowUp/Down/Home/End/Enter/Space/Esc/Tab
 *   - aria-activedescendant 跟踪 highlighted option（不抢焦点）
 *
 * Popover 用 Portal 渲染到 body，避免父 overflow:hidden 裁切 + z-index 干扰。
 */
import {
  useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState,
} from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown, Loader2 } from 'lucide-react'

export interface TransportOption<V extends string> {
  value: V
  label: string
  /** 副标题（可选，副文案如"OpenAI Chat Completions /v1/chat/completions"） */
  hint?: string
}

interface Props<V extends string> {
  value: V
  options: TransportOption<V>[]
  onChange: (next: V) => void
  disabled?: boolean
  busy?: boolean
  /** 左侧静态 label（"协议"），可选，省去时只显示当前值 */
  label?: string
  /** 触发器 title 提示 */
  title?: string
  /** 触发器 aria-label，无 visible label 时必填 */
  ariaLabel?: string
}

export default function TransportDropdown<V extends string>({
  value, options, onChange, disabled, busy, label, title, ariaLabel,
}: Props<V>) {
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState<number>(() =>
    Math.max(0, options.findIndex((o) => o.value === value)),
  )
  const triggerRef = useRef<HTMLButtonElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)
  const listboxId = useId()

  const current = useMemo(
    () => options.find((o) => o.value === value) ?? options[0],
    [options, value],
  )

  /* ── popover 定位：跟随 trigger 的 bounding rect ── */
  const [pos, setPos] = useState<{ top: number; left: number; width: number } | null>(null)
  const recalcPos = useCallback(() => {
    const t = triggerRef.current
    if (!t) return
    const r = t.getBoundingClientRect()
    setPos({
      top: r.bottom + 4,
      left: r.left,
      // popover 宽度跟 trigger 严格一致
      width: r.width,
    })
  }, [])
  useLayoutEffect(() => {
    if (!open) return
    recalcPos()
    const onScrollOrResize = () => recalcPos()
    window.addEventListener('scroll', onScrollOrResize, true)
    window.addEventListener('resize', onScrollOrResize)
    return () => {
      window.removeEventListener('scroll', onScrollOrResize, true)
      window.removeEventListener('resize', onScrollOrResize)
    }
  }, [open, recalcPos])

  /* ── 打开时 highlight 同步到当前值 ── */
  useEffect(() => {
    if (open) {
      setHighlight(Math.max(0, options.findIndex((o) => o.value === value)))
    }
  }, [open, options, value])

  /* ── 点击外部关闭 ── */
  useEffect(() => {
    if (!open) return
    const onDocPointer = (e: PointerEvent) => {
      const t = e.target as Node
      if (triggerRef.current?.contains(t)) return
      if (popoverRef.current?.contains(t)) return
      setOpen(false)
    }
    document.addEventListener('pointerdown', onDocPointer)
    return () => document.removeEventListener('pointerdown', onDocPointer)
  }, [open])

  /* ── 键盘交互 ── */
  const onTriggerKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    if (disabled) return
    if (!open) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        setOpen(true)
      }
      return
    }
    // open 状态下：ArrowDown/Up 移 highlight；Enter 确认；Esc 关；Home/End 跳头尾
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlight((h) => (h + 1) % options.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlight((h) => (h - 1 + options.length) % options.length)
    } else if (e.key === 'Home') {
      e.preventDefault()
      setHighlight(0)
    } else if (e.key === 'End') {
      e.preventDefault()
      setHighlight(options.length - 1)
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      const opt = options[highlight]
      if (opt) {
        if (opt.value !== value) onChange(opt.value)
        setOpen(false)
      }
    } else if (e.key === 'Escape') {
      e.preventDefault()
      setOpen(false)
    } else if (e.key === 'Tab') {
      setOpen(false)
    }
  }

  const optionId = (i: number) => `${listboxId}-opt-${i}`

  const popover = open && pos ? (
    <div
      ref={popoverRef}
      className="lr-transport-dd__popover"
      role="listbox"
      id={listboxId}
      aria-activedescendant={highlight >= 0 ? optionId(highlight) : undefined}
      style={{
        position: 'fixed',
        top: pos.top,
        left: pos.left,
        minWidth: pos.width,
      }}
    >
      {options.map((opt, i) => {
        const selected = opt.value === value
        const active = i === highlight
        return (
          <div
            key={opt.value}
            id={optionId(i)}
            role="option"
            aria-selected={selected}
            className={
              'lr-transport-dd__option' +
              (selected ? ' is-selected' : '') +
              (active ? ' is-active' : '')
            }
            onMouseEnter={() => setHighlight(i)}
            onPointerDown={(e) => {
              // 用 pointerdown 而非 click：避免被 document.pointerdown 关闭逻辑抢先
              e.preventDefault()
              if (opt.value !== value) onChange(opt.value)
              setOpen(false)
              triggerRef.current?.focus()
            }}
          >
            <div className="lr-transport-dd__option-main">
              <div className="lr-transport-dd__option-label">{opt.label}</div>
              {opt.hint && <div className="lr-transport-dd__option-hint">{opt.hint}</div>}
            </div>
            {selected && <Check size={13} className="lr-transport-dd__option-check" />}
          </div>
        )
      })}
    </div>
  ) : null

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="lr-transport-dd__trigger"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-label={ariaLabel}
        title={title}
        disabled={disabled}
        data-busy={busy ? '1' : undefined}
        data-open={open ? '1' : undefined}
        onClick={() => !disabled && setOpen((v) => !v)}
        onKeyDown={onTriggerKeyDown}
      >
        {label && <span className="lr-transport-dd__label">{label}</span>}
        <span className="lr-transport-dd__value">{current?.label ?? ''}</span>
        {busy
          ? <Loader2 size={11} className="spin lr-transport-dd__icon" />
          : <ChevronDown size={12} className={'lr-transport-dd__icon' + (open ? ' is-open' : '')} />
        }
      </button>
      {popover && createPortal(popover, document.body)}
    </>
  )
}
