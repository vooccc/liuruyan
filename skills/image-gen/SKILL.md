---
name: image-gen
description: "文生图 / 图生图。用户让你画图、生成图片、改图、做插画/图标/Logo 时使用。调用已配置的生图模型（agnes-image / DALL-E / flux 等），生成的图片直接渲染在聊天里。"
description_en: "Text-to-image and image-to-image generation. Use when the user asks you to draw, generate, or edit an image."
---

# 生图技能 (image-gen)

调用 OpenAI 兼容的 `/v1/images/generations` 端点生成图片。支持**文生图**和**图生图**（图像编辑）。

生成的图片会**直接渲染在聊天界面**（图片卡片），无需你贴 URL 或 base64。

**重要：即使当前聊天模型无 vision 能力，也可以用本技能处理图片。**
- 文生图：不需要 vision，直接根据描述生成
- 图生图：不需要 vision，脚本会自动读取图片文件并发给生图 API

---

## 何时使用

- 用户让你「画一张…」「生成一张图」「做个 Logo / 图标 / 插画」
- 用户描述一个视觉概念想看到效果
- 用户发了图让你「改成水彩画」「加点东西」「换个风格」「改颜色」（图生图）
- 当前模型无 vision 能力 + 用户要求处理图片 → 优先考虑本技能，不要说“我无法处理图片”

## 何时不用

- 用户让你读取/识别已有图片的内容 → 用 `read_file`（需 vision 模型）
- 纯文字任务

---

## 前置条件

用户需要在柳如烟「设置 → 模型管理」里添加至少一个生图模型，例如：
- `agnes-image-2.1-flash`（文生图 + 图生图，免费，推荐）
- `agnes-image-2.0-flash`（图生图/图像编辑）
- `dall-e-3` / `gpt-image-1`
- flux / Stable Diffusion 系列

脚本会自动从柳如烟配置里读取该模型的 `base_url` 和 `api_key`，你不需要操心密钥。

---

## 用法

通过 `execute_command` 调用脚本。脚本路径固定为：

```bash
python "skills/image-gen/scripts/generate_image.py" --prompt "一只橘猫在月光下的屋顶上弹钢琴，水彩画风格，柔和的光晕"
```

指定 Agnes 模型：

```bash
python "skills/image-gen/scripts/generate_image.py" --prompt "赛博朋克风格的上海外滩夜景" --model agnes-image-2.1-flash --size 1024x1024
```

指定服务商（当多个服务商都有同名模型时使用，支持 provider id 或名称）：

```bash
python "skills/image-gen/scripts/generate_image.py" --prompt "蓝色圆形极简图标，白底" --model gpt-image-2 --provider 浮梦 --size 1024x1024
```

图生图 / 图像编辑：

```bash
python "skills/image-gen/scripts/generate_image.py" --prompt "改成水彩画风格" --model agnes-image-2.1-flash --image "path/to/photo.png"
```

---

## 参数

| 参数 | 必填 | 说明 |
|------|------|------|
| `--prompt` | 是 | 图片描述，越详细越好（主体、风格、构图、色彩、氛围） |
| `--model` | 否 | 生图模型 id。留空自动检测第一个可用的生图模型 |
| `--provider` | 否 | 服务商 id 或名称。默认使用第一个匹配服务商；同名模型多家可用时建议显式指定 |
| `--size` | 否 | 尺寸，如 `1024x1024` / `1792x1024` / `1024x1792`。默认 `1024x1024` |
| `--image` | 否 | 输入图片路径或 URL（图生图模式）。本地路径会自动读文件转 base64 |
| `--n` | 否 | 生成数量 1-4。多数模型只支持 1 |
| `--format` | 否 | 留空则不主动传 `response_format`；可选 `url` / `b64_json`。Agnes 默认 `url`，`gpt-image-2` 建议留空 |
| `--style` | 否 | 风格提示 `vivid` / `natural`（DALL-E 3） |
| `--negative-prompt` | 否 | 负向提示词（SD/flux 系列支持） |

---

## 输出与渲染

脚本输出一段 JSON 到 stdout。成功时 JSON 含 `"_runtime_block": "generated_image"`，柳如烟前端会自动识别并把图片渲染成卡片。

拿到结果后：
- 成功：简短描述你生成了什么，不要把 URL / base64 原样贴出来，图片已经显示了
- 失败（JSON 含 `error` 字段）：读懂错误告诉用户，不要盲目重试同样的调用

常见错误：

| error 内容 | 原因 / 处理 |
|------|------|
| 找不到拥有模型 'xxx' 的 provider | 模型没在设置里添加，提示用户去添加 |
| 没有找到可用的生图模型 | 用户一个生图模型都没配 |
| 生图 API 返回错误 4xx | 上游报错，看具体 message（可能是参数不支持/额度用尽） |
| 图片文件不存在 | `--image` 路径错了 |

---

## 工作原理

1. 当前聊天模型不需要是生图模型，本脚本独立调生图 API
2. 脚本读 `models.json` + `models.secrets.json` 找到生图模型的 provider 配置
3. 默认使用第一个匹配 provider；传 `--provider` 时只使用指定 provider
4. 文生图只发 `prompt`；图生图额外发 `image`（base64）+ `tags:["img2img"]`（Agnes 协议）
5. 返回的图片 JSON 带 sentinel，前端渲染成卡片

---

**Skill by:** 柳如烟内置
