/**
 * 模型分组（对齐 通用供应商规范 的批量获取路径）。
 *
 * 关键澄清：cherry 有两套分组逻辑，对应两条不同 code path：
 *   1. listModels.ts::defaultGroup —— 点"获取模型"批量拉 /v1/models 时用。
 *      规则极简：split('/')[0]，没 '/' 就 fallback = providerId。
 *   2. naming.ts::getDefaultGroupName —— 用户在 AddModelPopup 手动加单个
 *      模型时用，是更细的启发式（gpt-4.1 / gpt-4o 各成一组）。
 *
 * 我们这里是"获取模型后展示模型列表"，对应的是路径 1（listModels）。
 * 之前误用了路径 2 的启发式，导致 getrouter 这类聚合 router 切出
 * 一堆 gpt-4.1 / gpt-4o / gpt-5 / claude-3 / claude-opus 小组，
 * 跟 cherry 实际行为不一致。
 *
 * 参考：references/provider-models
 */

/** 跟 cherry 同名同行为：按分隔符切 model id 取第一段当 group。
 *
 * - 默认：先用 ['/', ' ', ':'] 切；都没命中再用 ['-', '_'] 取前两段
 *   - 'anthropic/claude-3.5-sonnet' → 'anthropic'
 *   - 'gpt-4.1-mini'                → 'gpt-4.1'
 *   - 'o3'                          → 'o3'（无分隔符 fallback）
 * - 聚合 provider（aihubmix/silicon/ocoolai/o3/dmxapi）激进归拢：所有
 *   分隔符都当一级，gpt-4o / gpt-4.1 / gpt_4.1 都归到 'gpt'
 */
export function getDefaultGroupName(id: string, provider?: string): string {
  const str = id.toLowerCase()

  let firstDelimiters = ['/', ' ', ':']
  let secondDelimiters = ['-', '_']

  if (provider && ['aihubmix', 'silicon', 'ocoolai', 'o3', 'dmxapi'].includes(provider.toLowerCase())) {
    firstDelimiters = ['/', ' ', '-', '_', ':']
    secondDelimiters = []
  }

  for (const d of firstDelimiters) {
    if (str.includes(d)) return str.split(d)[0]
  }
  for (const d of secondDelimiters) {
    if (str.includes(d)) {
      const parts = str.split(d)
      return parts.length > 1 ? parts[0] + '-' + parts[1] : parts[0]
    }
  }
  return str
}

/** 跟 供应商模型发现与列表解析 同行为：
 *  - id 包含 '/' → 取 '/' 前那一段（'anthropic/claude-3.5' → 'anthropic'）
 *  - 否则 → fallback = providerId（'gpt-4o' / 'claude-3-5-sonnet' → providerId）
 *
 * 参考：references/provider-models
 */
export function getListModelsGroupName(id: string, providerId?: string): string {
  const parts = id.split('/')
  if (parts.length > 1 && parts[0]) return parts[0].toLowerCase()
  return (providerId || id).toLowerCase()
}

/** 把模型列表按 group 分组，保持组内原顺序。
 *
 * 分组键：完全对齐 cherry 的批量获取路径 listModels.ts::defaultGroup：
 * split('/')[0]，没 '/' 就 fallback = providerId。
 *
 * **不要**优先用 model.owned_by——聚合 router（getrouter / newapi / dmxapi）
 * 经常按渠道细分 owned_by，会导致每个版本各占一组。
 * **不要**用 getDefaultGroupName 那种启发式（gpt-4.1 / gpt-4o 各组）——
 * 那是 cherry 手动加单模型时用的，跟批量获取场景不匹配。
 *
 * 返回：{ groupKey: ModelLike[] }；组顺序 = 第一个属于该组的模型出现顺序。
 */
export function groupModelsByDefault<T extends { id: string; owned_by?: string }>(
  models: T[],
  providerId?: string,
): Record<string, T[]> {
  const groups: Record<string, T[]> = {}
  for (const m of models) {
    const key = getListModelsGroupName(m.id, providerId)
    if (!groups[key]) groups[key] = []
    groups[key].push(m)
  }
  return groups
}
