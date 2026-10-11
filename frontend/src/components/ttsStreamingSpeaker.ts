/**
 * TTS 句级流式朗读（M2 Streaming）。
 *
 * 设计目标
 * ========
 * 替代"等整段消息完成才合成"的延迟。流式过程中，每检测到一个完整句子边界
 * 就立刻提交合成，多个合成请求自然在 ttsAutoPlayer 队列里串行播放。
 * 用户从"等 5 秒文字 + 3 秒合成 = 8 秒沉默" → "1 秒后就能听到第一句"。
 *
 * 核心算法：findSafeBoundaries
 * ==========================
 * 在累积的全文中扫描"安全切分点"：
 * - 普通文本里：遇到句末标点（中英文 ``.!?。！？`` / 换行 ``\n``）即切
 * - ``<say>...</say>`` 内部：**绝对不切**——跨边界送给后端，should_speak 在
 *   mode=key 下会按 say 正则提取；切到 say 一半会让两半都不是合法 say 被过滤掉
 *
 * 状态机
 * ======
 * 每个 ``messageKey``（sid:messageId 复合）维护：
 * - ``buffer``：已 feed 进来的全部文本
 * - ``cursor``：已切出去过的字符位置——cursor 之前的内容已经送进合成队列
 *
 * 流结束时调 ``flush(messageKey)``：把 cursor 到末尾的"无标点尾巴"也一次送出去。
 * 切走会话或用户停止时调 ``reset(messageKey)`` 释放状态。
 *
 * 零阻塞契约
 * ==========
 * - feed/flush 只往全局 ``ttsAutoPlayer`` 队列推合成请求，不返回 Promise，调用方
 *   不用 await。合成失败只 console.warn，不向上传播。
 * - 状态完全本地（Map），不动 store/Redux/Recoil。重渲染零影响。
 *
 * 设计文档：``docs/architecture_spec.md`` §M2-Streaming
 */
import { getTTSConfig, synthesizeText, TTSConfigView, TTSSpeakMode } from '../api/tts'
import { ttsAutoPlayer } from './ttsAutoPlayer'

/* ── 配置与缓存 ─────────────────────────────────────────────── */

const DEFAULT_CONFIG: TTSConfigView = {
  enabled: false,
  mode: 'off',
  provider_type: 'openai_compat',
  provider_id: '',
  base_url: '',
  has_api_key: false,
  model: 'tts-1',
  voice: 'alloy',
  response_format: 'mp3',
  speed: 1.0,
  gptsovits_base_url: 'http://127.0.0.1:9880',
  gptsovits_refer_wav_path: '',
  gptsovits_prompt_text: '',
  gptsovits_text_lang: 'zh',
  tencent_secret_id: '',
  has_tencent_secret_key: false,
  tencent_voice_type: 101001,
  tencent_region: 'ap-guangzhou',
  baidu_api_key: '',
  has_baidu_secret_key: false,
  baidu_voice: 0,
  baidu_speed: 5,
  baidu_pitch: 5,
  baidu_volume: 5,
  baidu_cuid: 'liuruyan',
  aliyun_access_key_id: '',
  has_aliyun_access_key_secret: false,
  aliyun_appkey: '',
  aliyun_voice: 'xiaoyun',
  aliyun_emotion: '',
  aliyun_region: 'cn-shanghai',
  aliyun_sample_rate: 16000,
  aliyun_volume: 50,
  aliyun_speech_rate: 0,
  aliyun_pitch_rate: 0,
  streaming_enabled: true,
  streaming_min_segment_chars: 12,
  streaming_sentence_enders: '.!?。！？\n',
  streaming_protect_say_tags: true,
  streaming_flush_tail_on_complete: true,
  timeout_sec: 60,
}

let cachedConfig: TTSConfigView | null = null
let configLoading = false

async function loadConfigIfNeeded(): Promise<TTSConfigView> {
  if (cachedConfig) return cachedConfig
  if (configLoading) {
    while (configLoading) {
      await new Promise((resolve) => setTimeout(resolve, 50))
      if (cachedConfig) return cachedConfig
    }
  }
  configLoading = true
  try {
    const res = await getTTSConfig()
    cachedConfig = res.config
  } catch (e) {
    console.warn('[TTSStreaming] failed to load tts config, using default:', e)
    return DEFAULT_CONFIG
  } finally {
    configLoading = false
  }
  return cachedConfig || DEFAULT_CONFIG
}

/** 供外部调用：在会话/生成开始时，预先异步拉取/加载最新的 TTS 配置。 */
export async function ensureTtsStreamingConfig(): Promise<void> {
  await loadConfigIfNeeded()
}

/** 供配置面板保存成功时调用：失效当前前端配置缓存，下次朗读/生成时重新拉取最新配置。 */
export function invalidateTtsStreamingConfig(): void {
  cachedConfig = null
}

/* ── 串行/并发请求限流队列 ───────────────────────────────────────── */

class TaskQueue {
  private pending: Array<() => Promise<void>> = []
  private running = 0
  private maxConcurrency = 1

  constructor(maxConcurrency = 1) {
    this.maxConcurrency = maxConcurrency
  }

  async add<T>(fn: () => Promise<T>): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const task = async () => {
        try {
          const res = await fn()
          resolve(res)
        } catch (e) {
          reject(e)
        } finally {
          this.running--
          this._next()
        }
      }
      this.pending.push(task)
      this._next()
    })
  }

  private _next() {
    if (this.running >= this.maxConcurrency) return
    const task = this.pending.shift()
    if (task) {
      this.running++
      void task()
    }
  }

  clear() {
    this.pending = []
  }
}

// 本地单线程/单 GPU 服务限流为 1 串行，彻底避免 HTTP 排队超时
const gptsovitsQueue = new TaskQueue(1)
// 云端多路支持并发，设为 3 提高效率
const openaiQueue = new TaskQueue(3)

function getQueueForProvider(providerType: string) {
  return providerType === 'gptsovits_compat' ? gptsovitsQueue : openaiQueue
}

/* ── 正在合成中的段落数计数器 ───────────────────────────────────────── */

let activeSynthCount = 0
const synthListeners = new Set<(count: number) => void>()

export function subscribeSynthCount(listener: (count: number) => void): () => void {
  synthListeners.add(listener)
  listener(activeSynthCount)
  return () => {
    synthListeners.delete(listener)
  }
}

function changeSynthCount(delta: number) {
  activeSynthCount = Math.max(0, activeSynthCount + delta)
  for (const l of synthListeners) {
    try {
      l(activeSynthCount)
    } catch (e) {
      console.warn('[TTSStreaming] synthCount listener error:', e)
    }
  }
}

// 试听专用音频片段缓存器：用来在流式播放结束后拼接成整段完整音频供用户下载/回放
let auditionBlobs: Blob[] = []

export function getAuditionBlobs(): Blob[] {
  return auditionBlobs
}

export function clearAuditionBlobs(): void {
  auditionBlobs = []
}

// 模块加载时预热拉取一次配置，尽量消除首次冷启动时差
void loadConfigIfNeeded()

/* ── 边界检测 ─────────────────────────────────────────────── */

const SAY_OPEN = '<say>'
const SAY_CLOSE = '</say>'

/**
 * 在 ``text`` 的 ``[fromIdx, end)`` 区间扫描"安全切分点"位置（绝对索引，指向
 * 切分点之**后**的位置——也就是切片的右边界）。
 *
 * 返回的边界一定是"完整的"——cursor 到任意一个 boundary 之间的子串可以独立
 * 送去合成而不破坏 say 标签语义。
 *
 * @param text 已累积的全文
 * @param fromIdx 从这个位置开始扫（cursor）
 * @param enders 句末标点符号字符串
 * @param protectSay 是否在 say 标签内进行边界保护
 *
 * @example 关键场景（每条都应作为回归参考）
 * - 普通中文：``findSafeBoundaries("你好。世界。", 0)`` → ``[3, 6]``
 * - 英文标点：``findSafeBoundaries("Hi. There.", 0)`` → ``[3, 10]``
 * - 跨 say 边界保护：
 *     ``findSafeBoundaries("<say>你好。哈哈</say>后续。", 0)`` → ``[16, 19]``
 *     （绝不在 say 内部的 ``。`` 切，等到 ``</say>`` 闭合再切）
 * - say 未闭合：
 *     ``findSafeBoundaries("<say>你好。", 0)`` → ``[]``
 *     （等更多文本到达才能确定边界）
 * - 切到一半的 say：
 *     ``findSafeBoundaries("前。<say>", 0)`` → ``[2]``
 *     （前面的 ``。`` 是合法切点，进 say 后没闭合不再切）
 * - 换行也算切点：
 *     ``findSafeBoundaries("第一行\n第二行。", 0)`` → ``[4, 8]``
 */
export function findSafeBoundaries(
  text: string,
  fromIdx: number,
  enders: string = '.!?。！？\n',
  protectSay: boolean = true,
): number[] {
  const boundaries: number[] = []
  let i = fromIdx
  let inSay = false
  const endersSet = new Set(enders.split(''))

  while (i < text.length) {
    if (!inSay) {
      // 检测 <say> 开始
      if (protectSay && text.startsWith(SAY_OPEN, i)) {
        inSay = true
        i += SAY_OPEN.length
        continue
      }
      // 句末标点：切到标点之后
      if (endersSet.has(text[i])) {
        boundaries.push(i + 1)
      }
      i++
    } else {
      // say 内部：找 </say> 闭合
      const end = text.indexOf(SAY_CLOSE, i)
      if (end < 0) {
        // 还没闭合——返回到目前为止的边界，等更多文本
        return boundaries
      }
      // </say> 闭合点之后是切分点
      i = end + SAY_CLOSE.length
      boundaries.push(i)
      inSay = false
    }
  }
  return boundaries
}

/* ── 状态 ──────────────────────────────────────────────────── */

interface MessageState {
  buffer: string
  cursor: number
  /** 用于异步 await 后比对是否已切走会话；> 0 表示该消息仍 active */
  generation: number
  agentId?: string
}

const states = new Map<string, MessageState>()

/** sid + messageId 拼成唯一 key。 */
function keyOf(sid: string, messageId: string | number): string {
  return `${sid}::${messageId}`
}

/* ── 公共 API ──────────────────────────────────────────────── */

export interface FeedOptions {
  sessionId: string
  messageId: string | number
  /** 当前消息的"完整最新文本"——调用方传增量后累积的全文，本模块自己跟自己的
   *  buffer diff 出新增部分。**不要传 delta**：累积语义更稳，跨多帧调用时不依赖
   *  调用方记录 cursor。 */
  fullText: string
  agentId?: string
  modeOverride?: TTSSpeakMode
}

/** 喂入最新累积文本，自动切句 + 提交合成。同步返回（不 await 合成）。 */
export function feedTtsStreaming(opts: FeedOptions): void {
  const { sessionId, messageId, fullText, agentId, modeOverride } = opts
  const config = cachedConfig || DEFAULT_CONFIG

  const activeMode = modeOverride || config.mode

  console.debug('[TTSStreaming] feed incoming:', {
    sessionId,
    messageId,
    fullTextLen: fullText.length,
    configEnabled: config.enabled,
    configMode: activeMode,
    streamingEnabled: config.streaming_enabled,
    cachedConfigLoaded: !!cachedConfig,
  })

  // 1. 如果全局 TTS 没启用且没有强制模式，或者朗读模式为 off，直接 return
  if ((!config.enabled && !modeOverride) || activeMode === 'off') {
    return
  }

  // 2. 如果流式被禁用，仅缓存 fullText，留到 flush 时一次性整段合成
  if (!config.streaming_enabled) {
    const key = keyOf(sessionId, messageId)
    let state = states.get(key)
    if (!state) {
      state = { buffer: '', cursor: 0, generation: 1, agentId }
      states.set(key, state)
    }
    state.buffer = fullText
    state.agentId = agentId
    console.debug('[TTSStreaming] streaming disabled; holding fullText for end flush')
    return
  }

  const key = keyOf(sessionId, messageId)
  let state = states.get(key)
  if (!state) {
    state = { buffer: '', cursor: 0, generation: 1, agentId }
    states.set(key, state)
  }
  // 新累积文本必须以旧 buffer 为前缀（流式增量天然如此）；万一不是（ex：用户重发
  // 编辑过的消息复用 messageId），重置 cursor 到 0 重新切。
  if (!fullText.startsWith(state.buffer)) {
    state.buffer = ''
    state.cursor = 0
  }
  state.buffer = fullText
  state.agentId = agentId

  // 开始获取切句边界
  const boundaries = findSafeBoundaries(
    state.buffer,
    state.cursor,
    config.streaming_sentence_enders,
    config.streaming_protect_say_tags,
  )

  console.debug('[TTSStreaming] boundaries found:', {
    cursor: state.cursor,
    boundaries,
    buffer: state.buffer,
  })

  if (boundaries.length === 0) return

  // 把 cursor 到最后一个边界之间的片段按 boundaries 切片送出
  let prev = state.cursor
  for (const b of boundaries) {
    const segment = state.buffer.slice(prev, b)
    const isSay = segment.includes('<say>')
    if (!isSay && !modeOverride && segment.trim().length < config.streaming_min_segment_chars) {
      console.debug('[TTSStreaming] slice skipped (too short):', {
        segment,
        len: segment.trim().length,
        min: config.streaming_min_segment_chars,
      })
      continue
    }
    prev = b
    console.debug('[TTSStreaming] enqueue segment:', { segment })
    void _enqueueSegment(segment, agentId, state.generation, key, activeMode)
  }
  state.cursor = prev

  // 如果此时没有在后台加载 config，预加载一次
  if (!cachedConfig && !configLoading) {
    void loadConfigIfNeeded()
  }
}

/** 流结束时调：把 cursor 到末尾的尾巴（可能没标点）一并送出，然后清状态。 */
export function flushTtsStreaming(opts: {
  sessionId: string
  messageId: string | number
  agentId?: string
}): void {
  const { sessionId, messageId, agentId } = opts
  const key = keyOf(sessionId, messageId)
  const state = states.get(key)
  if (!state) return

  const config = cachedConfig || DEFAULT_CONFIG

  // 1. 如果全局禁用，或者朗读模式为 off，什么都不做
  if (!config.enabled || config.mode === 'off') {
    states.delete(key)
    return
  }

  // 2. 如果流式被禁用：在结束时，我们需要一次性合成整段 buffer
  if (!config.streaming_enabled) {
    const fullText = state.buffer.trim()
    if (fullText) {
      void _enqueueSegment(fullText, agentId ?? state.agentId, state.generation, key, config.mode)
    }
    states.delete(key)
    return
  }

  // 3. 如果流式启用：只合成没切完的"尾巴"
  if (config.streaming_flush_tail_on_complete) {
    const tail = state.buffer.slice(state.cursor)
    if (tail.trim()) {
      void _enqueueSegment(tail, agentId ?? state.agentId, state.generation, key, config.mode)
    }
  }
  states.delete(key)
}

/** 切会话/用户停止时调：清掉该会话所有 message 的状态，停止已入队但未播的音频。 */
export function resetTtsStreaming(sessionId?: string): void {
  // 停止播放并清空播放队列
  ttsAutoPlayer.stop()

  // 同时也清空正在排队的合成请求，不再浪费算力和额度去合成不会被播放的文字
  gptsovitsQueue.clear()
  openaiQueue.clear()

  // 立即将合成计数器归零
  changeSynthCount(-99999)

  // 同时也清空试听专用缓存
  if (!sessionId || sessionId === 'audition') {
    clearAuditionBlobs()
  }

  if (!sessionId) {
    // 全清
    for (const state of states.values()) {
      state.generation += 1 // 让 in-flight 的 enqueue 失效
    }
    states.clear()
    return
  }
  const prefix = `${sessionId}::`
  for (const [k, v] of states) {
    if (k.startsWith(prefix)) {
      v.generation += 1
      states.delete(k)
    }
  }
}

/* ── 内部 ─────────────────────────────────────────────────── */

async function _enqueueSegment(
  segment: string,
  agentId: string | undefined,
  expectedGeneration: number,
  key: string,
  mode?: TTSSpeakMode,
): Promise<void> {
  const trimmed = segment.trim()
  if (!trimmed) return

  const config = cachedConfig || DEFAULT_CONFIG
  const queue = getQueueForProvider(config.provider_type)

  changeSynthCount(1)
  try {
    // 把合成请求丢进根据 provider 选定的限流队列里串行/并发执行
    const blob = await queue.add(() => synthesizeText(trimmed, { agentId, mode }))

    // 异步排队等待/合成期间用户可能切走/重发——比对 generation 防止 stale 写入播放器
    const cur = states.get(key)
    if (cur && cur.generation !== expectedGeneration) return

    if (blob) {
      if (key === 'audition::preview') {
        auditionBlobs.push(blob)
      }
      ttsAutoPlayer.enqueue(blob)
    }
  } catch (e) {
    // 单句失败不影响后续——console.warn 即可
    console.warn('[TTSStreaming] synth failed for segment:', e)
  } finally {
    changeSynthCount(-1)
  }
}
