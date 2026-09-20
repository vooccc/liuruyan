# Visual Style

Use this reference when generating Guizang-style labeled material illustrations.

## Default Look

Use a clean Swiss editorial 3D illustration style:

- Off-white studio background.
- Black ink outlines and subtle gray physical surfaces.
- One vivid accent color used for arrows, dots, active blocks, and label connectors.
- Soft studio light, mild contact shadows, no dramatic gradients.
- Diagram objects should feel like small physical models, not flat app UI.
- The image should work as the central illustration inside a social card, slide, or article graphic.

Do not use:

- Decorative blobs, bokeh, gradient orbs, stock-photo backgrounds.
- Logos, watermarks, fake app chrome, fake brand marks.
- Dense legends or paragraph text inside the image.
- Tiny labels placed on busy surfaces.
- Pure poster decoration that does not explain the user's idea.
- Overly cute characters for serious business, scientific, financial, legal, or policy topics.

## Accent Colors

Pick one accent per image set. Do not mix accents inside the same set unless the user asks for comparison.

- IKB Blue `#002FA7`: default; best for technical systems, AI, workflows.
- Safety Orange `#FF6B35`: best for alerts, decision points, migration, risk, "watch this" content.
- Lemon Green `#C5E803`: best for growth, compounding loops, health, finance, optimization.
- Lemon Yellow `#FFD500`: best for teaching, summaries, highlights, beginner guides.
- Signal Red `#E60012`: use sparingly for failure states, blockers, or strict review gates.

In prompts, describe the accent in words and hex value, for example: `one vivid IKB blue accent (#002FA7)`.

## Ratios And Safe Area

Choose the ratio before prompting:

- Social card image well: `wide horizontal 1.9:1`, designed for a `936x500` image well.
- Slide/doc hero: `16:9`.
- Square post inset: `1:1`, but only if the final layout really needs square.

Always ask the image model for:

- Full subject visible.
- Generous safe margins on all sides.
- Labels away from edges.
- No crop.
- Centered vertically.

If the final card has a known image well, name it in the prompt.

## Text Inside The Image

Use image-generated text when the labels are part of the diagram. The labels should be printed callouts attached to objects or arrows.

Label rules:

- Use Simplified Chinese unless the user asks otherwise.
- Keep labels short: 2-5 Chinese characters.
- Use 3-5 labels per illustration.
- Use clear spatial placement: top-left, top-center, top-right, bottom-center, right edge.
- Ask for labels to be horizontal, large, high contrast, and readable.
- Place labels on quiet off-white areas or simple white callout plates.

Good labels:

- `用户提示`
- `AI 执行`
- `结果检查`
- `下一轮`
- `明确目标`
- `AI 尝试`
- `评估器`
- `未过重试`
- `时间触发`
- `AI 巡检`
- `外部系统`
- `等下次`

Avoid long labels:

- `系统自动化地执行任务`
- `根据评价结果进行下一轮迭代`
- `用户输入的提示词内容`

## Common Composition Patterns

- Cycle: arrange 3-4 objects in a circular flow with arrows.
- Pipeline: place objects left-to-right with arrows and a return line only if needed.
- Hub-and-spoke: put a routing hub in the middle, branches around it.
- Before/after: left state and right state, with a transformation object in the middle.
- Layer stack: vertical layers with labels beside each layer.

Prefer one simple metaphor per illustration. Do not combine a cycle, a dashboard, a factory, and a roadmap in one image.

## Tone Matching

Match the subject:

- Workplace reports should feel clear, calm, and credible.
- Creator posts can be more editorial and metaphorical, but still explain one idea.
- Education images can be friendly, but the mechanism should remain correct.
- Humanities images can be symbolic, but should not pretend uncertain details are factual.
