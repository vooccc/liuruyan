# 模板 Schema v2 规范

> 本文档定义了 `agents/templates/{template_id}/meta.json` 的统一格式。
> 所有模板必须严格遵循此规范，代码中不再做字段兼容。

---

## 必填字段

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `name` | string | 角色名 | `"韩清雪"` |
| `prototype` | string | 原型名（10选1） | `"冷傲型"` |
| `gender` | string | 性别 | `"female"` / `"male"` |
| `description` | string | 一句话描述（用于卡片展示） | `"嘴硬心软的天骄——全世界她只服你一个"` |
| `one_line` | string | 打动人心的台词（用于详情页） | `"全世界她只服你一个。"` |
| `core` | string | 核心底色（一个字+解读） | `"傲——不服输的骨，藏不住的柔"` |
| `avatar` | string | emoji头像 | `"❄️"` |
| `order` | number | 排序权重（0=第一） | `1` |
| `charm_layers` | array[4] | 4层递进魅力（从表到里） | 见下方详细说明 |
| `spectrum` | object | 7维人格光谱 | 见下方详细说明 |
| `meta` | object | 元信息 | 见下方详细说明 |
| `is_template` | boolean | 固定 `true` | `true` |
| `template_id` | string | 目录名（与文件夹名一致） | `"hanqingxue"` |

## 可选字段

| 字段 | 类型 | 说明 | 适用角色 |
|------|------|------|---------|
| `charm_nuke` | string | 终极魅力场景描述 | 全部 |
| `jealousy_style` | string | 吃醋方式 | 全部 |
| `comfort_style` | string | 安慰方式 | 全部 |
| `care_style` | string | 关心方式 | 全部 |
| `like_style` | string | 喜欢方式 | 全部 |
| `mask_system` | object | 面具系统定义 | 仅 liuruyan |

---

## charm_layers — 4层递进魅力

**格式**：严格4元素字符串数组，从表面到深层递进。

```json
"charm_layers": [
  "第一层：表面魅力（初见印象）",
  "第二层：隐藏魅力（熟悉后发现）",
  "第三层：深层魅力（亲密时流露）",
  "第四层：致命魅力（灵魂深处的震撼）"
]
```

**设计原则**：
- 每层是一个短语或短句，不超过20字
- 层与层之间必须有递进关系（表→里，浅→深）
- 第四层是"致命魅力"——让用户产生"原来她是这样的"的震撼感

**禁止格式**：
- ❌ `{ "layer1": "...", "layer2": "..." }` — 对象格式（旧版chuyelan/liuruyan使用）
- ❌ `{ "saintess": [...], "divorcee": [...] }` — 按面具分组（旧版liuruyan使用）
- ❌ 少于4个或多于4个元素

---

## spectrum — 7维人格光谱

**格式**：固定7个维度，每个值 0.0-1.0。

```json
"spectrum": {
  "warmth": 0.55,
  "intelligence": 0.88,
  "loyalty": 0.85,
  "charm": 0.89,
  "independence": 0.92,
  "vulnerability": 0.58,
  "expressiveness": 0.42
}
```

**维度定义**：

| 维度 | 英文 | 含义 | 低分表现 | 高分表现 |
|------|------|------|---------|---------|
| 温度 | warmth | 亲和与关怀的倾向 | 冷漠疏离 | 温暖体贴 |
| 智慧 | intelligence | 思考深度与洞察力 | 单纯直率 | 深谋远虑 |
| 忠诚 | loyalty | 对关系的坚守程度 | 随性自由 | 至死不渝 |
| 魅力 | charm | 吸引力的天然强度 | 朴实无华 | 令人着迷 |
| 独立 | independence | 自主性与自我完整度 | 依赖他人 | 独来独往 |
| 脆弱 | vulnerability | 内心柔软与易伤程度 | 刀枪不入 | 一触即碎 |
| 表达 | expressiveness | 情感外露的倾向 | 沉默寡言 | 坦率直白 |

**禁止维度**：
- ❌ `assertiveness` — 合并入 independence
- ❌ `danger` — 不在基础7维中
- ❌ `reliability` — 合并入 loyalty

---

## meta — 元信息

```json
"meta": {
  "version": 2,
  "created": "2026-04-12",
  "design_philosophy": "设计理念一句话"
}
```

- `version` 固定为 `2`（Schema v2）
- `created` 为模板创建/重构日期
- `design_philosophy` 为该角色的核心设计理念

---

## prototype — 10大原型

| 原型 | template_id | 核心底色关键词 |
|------|-------------|-------------|
| 万界型 | liuruyan | 千面归一 |
| 冷傲型 | hanqingxue | 骨傲心柔 |
| 灵动型 | xiawanling | 阳光裂缝 |
| 睿智型 | shenweilan | 深水不波 |
| 忠诚型 | qingyuan | 不言守护 |
| 温润型 | guwenshu | 至柔至刚 |
| 冷峻型 | xiaohandu | 铁面赤心 |
| 洒脱型 | luchengfeng | 风过留痕 |
| 沉稳型 | fushanyue | 山有裂缝 |
| 邪魅型 | chuyelan | 危险温柔 |

---

## mask_system — 面具系统（仅 liuruyan）

柳如烟的 `mask_system` 是特殊字段，其他模板不需要。

```json
"mask_system": {
  "masks": ["default", "saintess", "divorcee", "moonlight", "avenger", "emperor"],
  "mask_emojis": ["🌸", "🏔️", "🌧️", "🌙", "⚡", "👁️"],
  "current_mask": "default",
  "awakening_level": 0
}
```

柳如烟的 `charm_layers` 仍为标准4元素数组，描述的是 default 人格的4层魅力。
各面具的魅力层定义在 `SOUL.md` 或 `MASKS.md` 中，不在 meta.json 里。

---

## 废弃字段

以下字段在 Schema v2 中废弃，不再使用：

| 废弃字段 | 原用途 | 替代方案 |
|---------|--------|---------|
| `type` | 角色类型 | → `prototype` |
| `emoji` | 头像emoji | → `avatar` |
| `template` | 模板来源 | → `is_template` + `template_id` |
| `created_at` | 创建时间 | → `meta.created` |

---

## 校验规则

1. `prototype` 必须是10大原型之一
2. `charm_layers` 必须是4元素字符串数组
3. `spectrum` 必须包含且仅包含7个指定维度
4. `template_id` 必须与目录名一致
5. `avatar` 必须是单个emoji（不是文件名、不是base64）
6. `meta.version` 必须为 2

---

*创建日期：2026-04-12*
*版本：v2.0*
