/**
 * pollingLoop —— 不重叠（no-overlap）定时轮询。
 *
 * 为什么需要它：裸 setInterval 到点就发请求，不管上一次有没有回来。一旦浏览器
 * 每源连接被占满（HTTP/1.1 六连接 + 常驻 SSE + 流式 /chat），所有轮询请求开始排队，
 * 而定时器照旧到点入列 → 队列指数堆积。实测遥测里出现过几百个 /sessions 同时挂着、
 * 最长等 615 秒才完成，期间新的 POST /chat 光拿响应头就要 98 秒 —— 用户侧就是
 * 「点了发送没反应、界面像卡死」。
 *
 * 规则：
 *   1. 上一次还没结束 → 本次 tick 直接跳过（永不重叠，队列不会堆）。
 *   2. 页面不可见时不轮询，重新可见时立刻补一次（默认开启）。
 *   3. task 抛错不影响下一次 tick。
 */

interface PollingOptions {
  /** 立即执行一次（默认 true，等价于原来的「先 fetch 一次再 setInterval」写法） */
  immediate?: boolean
  /** 页面隐藏时暂停、恢复可见时立即补一次（默认 true） */
  pauseWhenHidden?: boolean
}

/**
 * 启动轮询，返回 stop()。典型用法：
 *   useEffect(() => startPolling(load, 15000), [load])
 */
export function startPolling(
  task: () => Promise<unknown> | unknown,
  intervalMs: number,
  options: PollingOptions = {},
): () => void {
  const { immediate = true, pauseWhenHidden = true } = options
  let inFlight = false
  let stopped = false

  const run = async (force = false) => {
    if (stopped || inFlight) return
    if (!force && pauseWhenHidden && typeof document !== 'undefined' && document.hidden) return
    inFlight = true
    try {
      await task()
    } catch {
      // 轮询失败静默：下一个 tick 会重试，callsite 自己已按需 log
    } finally {
      inFlight = false
    }
  }

  const timer = window.setInterval(() => { void run() }, intervalMs)

  // 从后台切回前台：立刻补一次，避免用户看到过期数据等下一个 tick
  const onVisible = () => {
    if (!document.hidden) void run(true)
  }
  if (pauseWhenHidden && typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisible)
  }

  if (immediate) void run(true)

  return () => {
    stopped = true
    window.clearInterval(timer)
    if (pauseWhenHidden && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisible)
    }
  }
}
