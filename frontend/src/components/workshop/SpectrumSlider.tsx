/**
 * SpectrumSlider — 带刻度的7维性格光谱滑块
 *
 * 用于属性Tab中编辑 spectrum（每维 -1.0 ~ 1.0）
 */
interface SpectrumDimension {
  key: string
  label: string
  left: string    // -1.0 标签（如 "外向"）
  right: string   // +1.0 标签（如 "内向"）
}

/** 预定义的7维光谱 */
export const SPECTRUM_DIMENSIONS: SpectrumDimension[] = [
  { key: 'extraversion', label: '内外向', left: '外向', right: '内向' },
  { key: 'gentleness',   label: '温柔度', left: '温柔', right: '冷酷' },
  { key: 'rationality',  label: '理性',   left: '理性', right: '感性' },
  { key: 'cheerfulness', label: '开朗',   left: '开朗', right: '忧郁' },
  { key: 'dominance',    label: '支配',   left: '主导', right: '顺从' },
  { key: 'formality',    label: '正式感', left: '正式', right: '随性' },
  { key: 'openness',     label: '开放',   left: '开放', right: '保守' },
]

interface SpectrumSliderProps {
  values: Record<string, number>
  onChange?: (key: string, value: number) => void
  /** 是否只读（高级模式关闭时） */
  disabled?: boolean
}

export default function SpectrumSlider({ values, onChange, disabled = false }: SpectrumSliderProps) {
  return (
    <div className="spectrum-sliders">
      <div className="spectrum-header">
        <span className="spectrum-title">🎚️ 性格光谱</span>
        <span className="spectrum-hint">每维范围 [-1.0, +1.0]</span>
      </div>
      <div className="spectrum-list">
        {SPECTRUM_DIMENSIONS.map(dim => {
          const val = dim.key in values ? values[dim.key] : 0
          const displayVal = Math.round(val * 100) / 100

          return (
            <div key={dim.key} className="spectrum-item">
              <div className="spectrum-label-row">
                <span className="spectrum-dim-label">{dim.label}</span>
                <span className={`spectrum-value ${disabled ? 'readonly' : ''}`}>
                  {displayVal > 0 ? '+' : ''}{displayVal}
                </span>
              </div>
              <div className="spectrum-slider-row">
                <span className="spectrum-end-label">{dim.left}</span>
                <input
                  type="range"
                  min="-1"
                  max="1"
                  step="0.05"
                  value={val}
                  onChange={e => onChange?.(dim.key, parseFloat(e.target.value))}
                  disabled={disabled}
                  className="spectrum-range"
                />
                <span className="spectrum-end-label">{dim.right}</span>
              </div>
              {/* 刻度线 */}
              <div className="spectrum-ticks">
                <span className="tick" style={{ left: '0%' }}>-1</span>
                <span className="tick" style={{ left: '25%' }}>-0.5</span>
                <span className="tick" style={{ left: '50%' }}>0</span>
                <span className="tick" style={{ left: '75%' }}>0.5</span>
                <span className="tick" style={{ left: '100%' }}>+1</span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
