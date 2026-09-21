/**
 * 主聊天页模型选择器状态管理（per-session 语义）。
 *
 * 设计原则（per-agent model routing 主线最后一块）：
 * - 默认：会话跟随 agent.active_model（角色级默认），未配则跟随 global current_model
 * - 用户在某会话顶栏切了模型 → **只对该会话生效**，不污染全局、不动其他会话
 * - 实现路径：切模型只更新本地 perSessionOverride[sid]，发送 chat 时 body 带
 *   model；后端 @main.py:1669-1672 自动把它持久化到 session.model 字段
 * - UI 显示用 selectedModel（override > effective fallback），但塞进 chat body
 *   的是 submitModel（override only）—— 这样无 override 的会话每次发送不会
 *   把 effective 写成隐式 override
 *
 * 入参可空：兼容尚无激活会话的边界（页面初始未选 session）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  CLEAR_SESSION_MODEL_OVERRIDES_EVENT,
  MODELS_CATALOG_CHANGED_EVENT,
  listModels,
  Model,
  ModelsResponse,
  Session,
} from '../api/chat'
import { getActiveModel } from '../api/providers'

export const OPEN_MODEL_SETTINGS_EVENT = 'liuruyan:open-model-settings'
/** CharacterWorkshop 改了 agent.active_model 后 dispatch，用来强制 useChatModelManager
 *  重新拉 effective（否则 activeAgentId 没变，useEffect 不会重跑，输入框下方
 *  显示的还是旧模型，要切走再切回来才更新）。
 *  detail.agentId：被修改的 agent id，可选；监听方目前只看自己当前激活 agent 是否
 *  匹配，不匹配也无害（只是多拉一次 effective）。 */
export const AGENT_ACTIVE_MODEL_CHANGED_EVENT = 'liuruyan:agent-active-model-changed'

interface UseChatModelManagerArgs {
  /** 当前激活会话 id；切换时触发 effective 重拉 + override 种子回灌 */
  activeSessionId?: string | null
  /** 当前会话归属的 agent id，用来拉 effective model */
  activeAgentId?: string | null
  /** 当前 Session 对象，读 session.model 作为 override 种子（页面刷新后回灌） */
  activeSession?: Session | null
}

export function useChatModelManager(args: UseChatModelManagerArgs = {}) {
  const { activeSessionId, activeAgentId, activeSession } = args

  const [customModels, setCustomModels] = useState<Model[]>([])
  // per-session 的 UI override：用户在该会话顶栏主动选过的 model id + provider
  // 优先级高于 session.model（已发送的 override）和 effective fallback
  const [perSessionOverride, setPerSessionOverride] = useState<
    Record<string, { model: string; provider: string }>
  >({})
  // effective fallback：当前 agent 的 effective model（无 override 时显示用）
  const [effectiveFallback, setEffectiveFallback] = useState<{ model: string; provider: string } | null>(null)
  // pending picker selection：用户在 activeSessionId 还没就绪时切的 picker 选择，
  // 等 activeSessionId 出现后由下面的 useEffect 自动迁移到 perSessionOverride。
  //
  // ⏱️ 必须用 state 而非 ref——submitModel/submitProvider 是普通 const，读 ref 不
  // 会触发重渲染也不会让 useChatSubmitFlow 的闭包拿到最新值。场景：
  //   1) 用户点角色卡片，activeSessionId 还是 null（会话未 hydrate）
  //   2) 切 picker → 写 pendingPickerSelection
  //   3) 用户立即发消息 → 后端创建 session，但此时 useChatSubmitFlow 闭包里的
  //      submitModel/submitProvider 还是 ''（原 ref 写入不触发重渲染）
  //   4) chat body 不带 model/provider → 后端走 global default
  // 改成 state 后：切 picker → setState → submitModel/submitProvider 重新计算
  // → useChatSubmitFlow 重新生成 callback → 发消息时闭包拿到正确值。
  const [pendingPickerSelection, setPendingPickerSelection] = useState<{ model: string; provider: string } | null>(null)
  const [showModelPicker, setShowModelPicker] = useState(false)
  const modelBtnRef = useRef<HTMLDivElement>(null)
  const pickerPanelRef = useRef<HTMLDivElement>(null)

  const getModelProvider = useCallback((model?: { provider?: string | null }) => {
    return model?.provider || ''
  }, [])

  const applyModelCatalog = useCallback((data: ModelsResponse) => {
    setCustomModels(data.custom_models_display || [])
    // 全局 current_model 作为 effective fallback 兜底（agent 无 active_model 时）。
    // 若已经从 /providers/active 拉到更精确的 effective，就不再覆盖。
    setEffectiveFallback((prev) => prev ?? (data.current_model
      ? { model: data.current_model, provider: data.current_provider || '' }
      : null))
  }, [])

  // 切到新会话时，把 session.model 作为 override 种子（如果该 sid 还没本地 override）。
  // 场景：页面刷新后 perSessionOverride 内存清空，靠 session.model 持久化字段回灌
  // 让用户上次选过的模型恢复显示。
  //
  // provider 字段策略（2026-05-12 修）：优先读 session.provider（后端持久化字段，跟
  // session.model 配对存的稳定 id），仅当老 session 缺该字段时 fallback 到
  // customModels.find by model id。fallback 路径有"同名 model 多 provider 走错家"
  // 风险，仅作为老数据兼容兜底。后端 lazy migration 也会在 from_meta_dict 里反查一次
  // 补上，所以这条 fallback 实际只在极端情况下触发。
  useEffect(() => {
    if (!activeSessionId) return
    const sessionModel = activeSession?.model
    if (!sessionModel) return
    setPerSessionOverride((prev) => {
      if (prev[activeSessionId]) return prev
      const sessionProvider = activeSession?.provider
        || customModels.find((m) => m.id === sessionModel)?.provider
        || ''
      return {
        ...prev,
        [activeSessionId]: {
          model: sessionModel,
          provider: sessionProvider,
        },
      }
    })
  }, [activeSessionId, activeSession?.model, activeSession?.provider, customModels])

  // 拉 effective model：agent 切换时重拉，或 CharacterWorkshop 改了 active_model
  // dispatch AGENT_ACTIVE_MODEL_CHANGED_EVENT 时也重拉。无 agent 时拉 global 当兜底。
  const refetchEffective = useCallback(() => {
    let cancelled = false
    const scope = activeAgentId ? 'effective' : 'global'
    getActiveModel(scope, activeAgentId || undefined)
      .then((res) => {
        if (cancelled) return
        if (res.model) {
          setEffectiveFallback({
            model: res.model,
            provider: res.provider_id || '',
          })
        }
      })
      .catch(() => { /* 拉不到就保留 listModels 兜底 */ })
    return () => { cancelled = true }
  }, [activeAgentId])

  useEffect(() => {
    return refetchEffective()
  }, [refetchEffective])

  // 监听 CharacterWorkshop 改 active_model 后的事件 → 立即重拉 effective，
  // 修复"改完角色模型后输入框下方显示不更新，必须切走再切回"的 bug。
  useEffect(() => {
    const handler = () => { refetchEffective() }
    window.addEventListener(AGENT_ACTIVE_MODEL_CHANGED_EVENT, handler)
    return () => window.removeEventListener(AGENT_ACTIVE_MODEL_CHANGED_EVENT, handler)
  }, [refetchEffective])

  // 当前会话 override（仅此会话有效，跨会话切换不带过去）
  const activeOverride = activeSessionId ? perSessionOverride[activeSessionId] : null

  // selectedModel / selectedProvider：UI 显示用（picker 高亮、按钮文字）
  // 优先级：activeOverride > pendingPickerSelection > effectiveFallback
  //   - activeOverride：该会话主动切过的模型
  //   - pendingPickerSelection：activeSessionId 未就绪时切的，让 UI 立即反映
  //   - effectiveFallback：**角色级默认**（绝对不能被用户切 picker 污染）
  // 之前 switchCustomModel 跳过写 perSessionOverride 的分支里干了 setEffectiveFallback，
  // 导致 effectiveFallback 被用户选择污染 → 同一角色点回去之后的新会话
  // （无 perSessionOverride）显示的不是角色默认而是上次切的 picker 值，且
  // submitModel 读不到 fallback → chat body 空 model → 后端静默走 global default。
  // 现在：picker UI 的“立即反映”靠 activeOverride / pendingPickerSelection 的
  // setState 触发重渲染，不再需要污染 effectiveFallback。
  const selectedModel = activeOverride?.model || pendingPickerSelection?.model || effectiveFallback?.model || ''
  const selectedProvider = activeOverride?.provider || pendingPickerSelection?.provider || effectiveFallback?.provider || ''

  // submitModel：发 chat 时塞 body.model 用——**仅 override 才有值**。
  // 无 override 时不传 model，让后端 resolve_call_model_config 走 agent / global
  // 优先级链路；这样用户没主动切的会话永远跟随角色级默认，不会被悄悄写死成 effective。
  // pendingPickerSelection 作为 fallback：用户在 activeSessionId 未就绪时切的
  // picker 选择临时存这里，发消息能立刻读到而不依赖迁移 effect 跑完。
  const submitModel = activeOverride?.model || pendingPickerSelection?.model || ''
  // submitProvider：与 submitModel 配对，发 chat 时塞 body.provider 用。
  // 后端用 (model, provider) 严格匹配 provider，避免"同 model id 跨多家 provider
  // 时按 modelId 反查永远命中第一家"的 bug（典型：getrouter 和 llmapi 都有
  // claude-opus-4-7，前端切了 llmapi 但只发 model 时后端永远走 getrouter）。
  // 仅 override 才有值，原因同 submitModel；fallback pendingPickerSelection 同 submitModel。
  const submitProvider = activeOverride?.provider || pendingPickerSelection?.provider || ''

  const selectedCustomModel = useMemo(() => {
    if (!selectedModel) return null
    return customModels.find((model) => (
      model.id === selectedModel &&
      (!selectedProvider || getModelProvider(model) === selectedProvider)
    )) || null
  }, [customModels, getModelProvider, selectedModel, selectedProvider])

  const currentModelMeta = selectedCustomModel

  const openModelSettings = useCallback(() => {
    setShowModelPicker(false)
    window.dispatchEvent(new CustomEvent(OPEN_MODEL_SETTINGS_EVENT))
  }, [])

  // 切模型：仅写 per-session override，不再调 /models/current（避免污染全局 +
  // 影响其他会话）。下次 chat 请求 useChatSubmitFlow 会读 submitModel 塞 body.model，
  // 后端 @main.py:1669-1672 自动持久化到 session.model 字段。
  const switchCustomModel = useCallback((model: Model) => {
    const provider = getModelProvider(model)
    setShowModelPicker(false)
    const pickedConfig = { model: model.id, provider }
    // ⚠️ 不再 setEffectiveFallback(pickedConfig)——effectiveFallback 语义是“角色级
    // 默认 model”，被用户切 picker 的值覆盖会造成：同一角色中途开新会话后
    // selectedModel 看着是上次切的值（污染）但 submitModel 为空——UI 与发送
    // 不一致，后端被迫走 global default。UI 立即反映靠 perSessionOverride /
    // pendingPickerSelection 的 setState 触发重渲染 + selectedModel 优先读它们即可。
    if (!activeSessionId) {
      // 还没有激活会话（页面刚加载尚未 hydrate） → 暂存到 state，让
      // submitModel/submitProvider 立即反映出选择 → 用户发消息时能拿到。
      // 后续 activeSessionId ready 后由下面迁移 useEffect 动作到 perSessionOverride。
      setPendingPickerSelection(pickedConfig)
      return
    }
    setPerSessionOverride((prev) => ({
      ...prev,
      [activeSessionId]: pickedConfig,
    }))
  }, [activeSessionId, getModelProvider])

  // 把"在 activeSessionId 还没就绪时切的 picker 选择"迁移到 perSessionOverride，
  // 让该 sid 的后续发消息走正规 activeOverride 路径。
  // 触发：activeSessionId 从 null 变成有值时，pendingPickerSelection 有值，
  // 就把它写到该 sid 的 override 并清空 pending。下次发消息 submitModel 能走
  // activeOverride 路径获得正确值（作为对 pending fallback 的升级）。
  useEffect(() => {
    if (!activeSessionId) return
    if (!pendingPickerSelection) return
    setPerSessionOverride((prev) => {
      // 不覆盖该 sid 已有的 override（用户中途又切过）
      if (prev[activeSessionId]) return prev
      return {
        ...prev,
        [activeSessionId]: pendingPickerSelection,
      }
    })
    setPendingPickerSelection(null)
  }, [activeSessionId, pendingPickerSelection])

  useEffect(() => {
    listModels()
      .then(applyModelCatalog)
      .catch(() => {})
  }, [applyModelCatalog])

  // 监听 ProviderSettings 改动 providers/models 后的事件 → 立即重拉 listModels()
  // 让聊天界面顶部的模型下拉列表实时反映新增/删除/改名，无需重启应用。
  // 修复：以前只在 mount 时拉一次，用户在设置里加了模型后必须重启才能看到。
  useEffect(() => {
    const handler = () => {
      listModels()
        .then(applyModelCatalog)
        .catch(() => {})
    }
    window.addEventListener(MODELS_CATALOG_CHANGED_EVENT, handler)
    return () => window.removeEventListener(MODELS_CATALOG_CHANGED_EVENT, handler)
  }, [applyModelCatalog])

  // 监听"重置所有会话模型"信号：CharacterWorkshop 调用后端清完 session.model
  // 后会 dispatch 这个事件，hook 把内存里的 perSessionOverride 也整体清空。
  // 整体清的副作用很轻：其它 agent 的 session 切回去时 useEffect@67 会从
  // session.model 自动重新种子（后端没动那些 session）。
  useEffect(() => {
    const handler = () => {
      setPerSessionOverride({})
    }
    window.addEventListener(CLEAR_SESSION_MODEL_OVERRIDES_EVENT, handler)
    return () => window.removeEventListener(CLEAR_SESSION_MODEL_OVERRIDES_EVENT, handler)
  }, [])

  useEffect(() => {
    if (!showModelPicker) return
    const handleClick = (event: MouseEvent) => {
      const buttonElement = modelBtnRef.current
      const panelElement = pickerPanelRef.current
      if (
        buttonElement &&
        !buttonElement.contains(event.target as Node) &&
        panelElement &&
        !panelElement.contains(event.target as Node)
      ) {
        setShowModelPicker(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [showModelPicker])

  return {
    customModels,
    selectedModel,
    selectedProvider,
    /** 仅当用户主动 override 时才有值；交给 useChatSubmitFlow 塞 chat body */
    submitModel,
    /** 与 submitModel 配对的 provider id，让后端按 (model, provider) 严格匹配 */
    submitProvider,
    currentModelMeta,
    getModelProvider,
    showModelPicker,
    setShowModelPicker,
    modelBtnRef,
    pickerPanelRef,
    switchCustomModel,
    openModelSettings,
  }
}
