/**
 * MCP server 调 elicit/create 时弹的表单。
 *
 * 后端 SSE event 形如：
 *   { type: 'mcp.elicitation.request', server_id, request_id, params }
 *
 * params 字段（来自 mcp 1.27 ElicitRequest*Params）：
 *   - mode: 'form' | 'url'
 *   - message: string  人类提示
 *   - requestedSchema: JSON Schema  字段描述
 *
 * 我们做最小可用版：只支持 form，按 schema.properties 渲染输入框。
 * 复杂 schema（嵌套、数组、anyOf）退化为 raw textarea + JSON.parse。
 */

import { useEffect, useMemo, useState } from 'react'
import { X } from 'lucide-react'
import { respondElicitation } from '../../../api/mcp'

interface ElicitationParams {
  mode?: string
  message?: string
  requestedSchema?: {
    type?: string
    properties?: Record<string, { type?: string; description?: string; enum?: string[] }>
    required?: string[]
  }
}

export interface ElicitationRequest {
  server_id: string
  request_id: string
  params: ElicitationParams
}

interface Props {
  request: ElicitationRequest
  onClose: () => void
}

export default function ElicitationDialog({ request, onClose }: Props) {
  const { params, server_id, request_id } = request
  const props = params.requestedSchema?.properties ?? {}
  const required = useMemo(
    () => new Set(params.requestedSchema?.required ?? []),
    [params.requestedSchema],
  )

  const [values, setValues] = useState<Record<string, unknown>>({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [rawJson, setRawJson] = useState('')
  const useRaw =
    !params.requestedSchema || params.requestedSchema.type !== 'object' || Object.keys(props).length === 0

  useEffect(() => {
    setValues({})
    setRawJson('')
    setError(null)
  }, [request_id])

  const update = (k: string, v: unknown) => setValues((prev) => ({ ...prev, [k]: v }))

  const send = async (action: 'accept' | 'decline' | 'cancel') => {
    setSubmitting(true)
    setError(null)
    try {
      let content: Record<string, unknown> | null = null
      if (action === 'accept') {
        if (useRaw) {
          if (rawJson.trim()) {
            try {
              content = JSON.parse(rawJson)
            } catch (e) {
              throw new Error(`JSON 格式错误：${e instanceof Error ? e.message : e}`)
            }
          } else {
            content = {}
          }
        } else {
          // 校验 required
          for (const k of required) {
            if (values[k] === undefined || values[k] === '') {
              throw new Error(`字段 "${k}" 为必填`)
            }
          }
          content = values
        }
      }
      await respondElicitation(request_id, action, content)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="mcp-modal-backdrop" onClick={() => void send('cancel')}>
      <div className="mcp-modal" onClick={(e) => e.stopPropagation()}>
        <header className="mcp-modal-head">
          <h3>{server_id} 请求确认</h3>
          <button type="button" className="mcp-icon-btn" onClick={() => void send('cancel')}>
            <X size={16} />
          </button>
        </header>
        <div className="mcp-modal-body">
          {params.message ? <p className="mcp-modal-hint">{params.message}</p> : null}

          {useRaw ? (
            <label className="mcp-form-row">
              <span>响应（JSON）</span>
              <textarea
                rows={6}
                value={rawJson}
                onChange={(e) => setRawJson(e.target.value)}
                placeholder='{"key":"value"}'
              />
            </label>
          ) : (
            Object.entries(props).map(([key, schema]) => (
              <label key={key} className="mcp-form-row">
                <span>
                  {key}
                  {required.has(key) ? <em className="mcp-required-mark"> *</em> : null}
                  {schema.description ? (
                    <span className="mcp-field-desc">　{schema.description}</span>
                  ) : null}
                </span>
                {schema.enum ? (
                  <select
                    value={(values[key] as string) ?? ''}
                    onChange={(e) => update(key, e.target.value)}
                  >
                    <option value="">（请选择）</option>
                    {schema.enum.map((opt) => (
                      <option key={opt} value={opt}>
                        {opt}
                      </option>
                    ))}
                  </select>
                ) : schema.type === 'boolean' ? (
                  <select
                    value={values[key] === undefined ? '' : String(values[key])}
                    onChange={(e) => update(key, e.target.value === 'true')}
                  >
                    <option value="">（请选择）</option>
                    <option value="true">true</option>
                    <option value="false">false</option>
                  </select>
                ) : schema.type === 'number' || schema.type === 'integer' ? (
                  <input
                    type="number"
                    value={(values[key] as number | string) ?? ''}
                    onChange={(e) =>
                      update(key, e.target.value === '' ? undefined : Number(e.target.value))
                    }
                  />
                ) : (
                  <input
                    type="text"
                    value={(values[key] as string) ?? ''}
                    onChange={(e) => update(key, e.target.value)}
                  />
                )}
              </label>
            ))
          )}

          {error ? <div className="mcp-error-banner">{error}</div> : null}
        </div>
        <footer className="mcp-modal-foot">
          <button
            type="button"
            className="mcp-btn-secondary"
            onClick={() => void send('decline')}
            disabled={submitting}
          >
            拒绝
          </button>
          <button
            type="button"
            className="mcp-btn-primary"
            onClick={() => void send('accept')}
            disabled={submitting}
          >
            {submitting ? '提交中…' : '确认'}
          </button>
        </footer>
      </div>
    </div>
  )
}
