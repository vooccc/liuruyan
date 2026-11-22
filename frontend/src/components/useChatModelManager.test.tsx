// @vitest-environment jsdom
import { act, createRef } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ChatModelPicker from './ChatModelPicker'
import { AgentModelSelector } from './CharacterWorkshop'
import { useChatModelManager } from './useChatModelManager'
import { MODELS_CATALOG_CHANGED_EVENT, type Model, type ModelsResponse } from '../api/chat'

vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)

const mockListModels = vi.fn<() => Promise<ModelsResponse>>()
const mockGetActiveModel = vi.fn<(scope: string, agentId?: string) => Promise<{ provider_id: string; model: string }>>()

vi.mock('../api/chat', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/chat')>()
  return {
    ...actual,
    listModels: () => mockListModels(),
  }
})

vi.mock('../api/providers', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/providers')>()
  return {
    ...actual,
    getActiveModel: (scope: string, agentId?: string) => mockGetActiveModel(scope, agentId),
  }
})

let dispose: (() => void) | undefined
afterEach(() => {
  dispose?.()
  dispose = undefined
  vi.clearAllMocks()
})

describe('ChatModelPicker & useChatModelManager', () => {
  it('never falls back to hardcoded GLM-5v-Turbo when model metadata is missing', async () => {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    const modelBtnRef = createRef<HTMLDivElement>()
    const pickerPanelRef = createRef<HTMLDivElement>()

    await act(async () => {
      root.render(
        <ChatModelPicker
          currentModelMeta={null}
          customModels={[]}
          selectedModel=""
          selectedProvider=""
          showModelPicker={false}
          modelBtnRef={modelBtnRef}
          pickerPanelRef={pickerPanelRef}
          getModelProvider={(m) => m?.provider || ''}
          onTogglePicker={vi.fn()}
          onSelectCustomModel={vi.fn()}
          onOpenModelSettings={vi.fn()}
        />,
      )
    })
    dispose = () => {
      act(() => root.unmount())
      container.remove()
    }

    expect(container.textContent).toContain('未选择模型')
    expect(container.textContent).not.toContain('GLM-5v-Turbo')

    await act(async () => {
      root.render(
        <ChatModelPicker
          currentModelMeta={null}
          customModels={[]}
          selectedModel="gemini-3.8-flash-high"
          selectedProvider="google-antigravity"
          showModelPicker={false}
          modelBtnRef={modelBtnRef}
          pickerPanelRef={pickerPanelRef}
          getModelProvider={(m) => m?.provider || ''}
          onTogglePicker={vi.fn()}
          onSelectCustomModel={vi.fn()}
          onOpenModelSettings={vi.fn()}
        />,
      )
    })

    expect(container.textContent).toContain('gemini-3.8-flash-high')
    expect(container.textContent).not.toContain('GLM-5v-Turbo')
  })

  it('refetches effective model when MODELS_CATALOG_CHANGED_EVENT is dispatched', async () => {
    const models: Model[] = [
      { id: 'old-model', name: 'Old Model', provider: 'openai', provider_name: 'OpenAI' },
      { id: 'gemini-4-hydrogen-high', name: 'Gemini 4 Hydrogen (High)', provider: 'google-antigravity', provider_name: 'Google Antigravity' },
    ]
    mockListModels.mockResolvedValue({
      models: [],
      custom_models_display: models,
      current_model: 'old-model',
      current_provider: 'openai',
    })
    mockGetActiveModel
      .mockResolvedValueOnce({ provider_id: 'openai', model: 'old-model' })
      .mockResolvedValueOnce({ provider_id: 'google-antigravity', model: 'gemini-4-hydrogen-high' })

    function Harness() {
      const mgr = useChatModelManager({ activeAgentId: 'ruyan', activeSessionId: null, activeSession: null })
      return (
        <div data-testid="state">
          {mgr.selectedProvider}:{mgr.selectedModel}:{mgr.currentModelMeta?.name || 'none'}
        </div>
      )
    }

    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)

    await act(async () => {
      root.render(<Harness />)
    })
    dispose = () => {
      act(() => root.unmount())
      container.remove()
    }

    expect(container.textContent).toBe('openai:old-model:Old Model')

    mockListModels.mockResolvedValue({
      models: [],
      custom_models_display: models,
      current_model: 'gemini-4-hydrogen-high',
      current_provider: 'google-antigravity',
    })

    await act(async () => {
      window.dispatchEvent(new CustomEvent(MODELS_CATALOG_CHANGED_EVENT))
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(mockGetActiveModel).toHaveBeenCalledTimes(2)
    expect(container.textContent).toBe('google-antigravity:gemini-4-hydrogen-high:Gemini 4 Hydrogen (High)')
  })

  it('AgentModelSelector surfaces disabled bound model instead of pretending global default', async () => {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    const onChange = vi.fn()

    await act(async () => {
      root.render(
        <AgentModelSelector
          providers={[
            {
              id: 'google-antigravity',
              name: 'Google Antigravity',
              base_url: '',
              api_key: '',
              api_key_prefix: '',
              transport: 'openai_chat',
              require_api_key: false,
              freeze_url: true,
              enabled: true,
              is_builtin: true,
              is_pro: true,
              description: '',
              icon: '',
              homepage: '',
              api_key_url: '',
              has_api_key: false,
              extra_headers: {},
              models: [
                {
                  id: 'gemini-4-hydrogen-high',
                  name: 'Gemini 4 Hydrogen (High)',
                  tags: ['vision', 'function_calling'],
                  icon: '',
                  added_by_user: false,
                },
              ],
            },
          ]}
          value={{ provider_id: 'google-antigravity', model: 'gemini-3.8-flash-high' }}
          onChange={onChange}
        />,
      )
    })
    dispose = () => {
      act(() => root.unmount())
      container.remove()
    }

    const select = container.querySelector('select') as HTMLSelectElement
    expect(select.value).toBe('google-antigravity::gemini-3.8-flash-high')
    expect(select.selectedOptions[0]?.textContent).toContain('gemini-3.8-flash-high')
    expect(select.selectedOptions[0]?.textContent).toContain('已禁用或移除')
  })
})
