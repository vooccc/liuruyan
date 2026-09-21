---
name: guizang-material-illustration
description: Generate Guizang-style material illustrations, labeled explanatory visuals, material-styled chart illustrations, and data-first editorial images from articles, notes, product concepts, workplace reports, creator posts, tutorials, school materials, humanities topics, science explanations, screenshots, or chart data. Use when the user asks for 配图, 带字插图, 解释图, 图解插画, 概念拆解图, 图表美化, 数据图美化, 3D 图表, 汇报配图, 内容配图, 小学课文配图, 生物/化学/物理解释图, 人文类配图, process/loop/system diagrams, or wants GPT-Image / image generation to create supporting images that can sit inside social cards, docs, slides, PPTs, or posts.
---

# Guizang Material Illustration

Create supporting illustrations from source text, screenshots, or chart data. The output is an image-generation prompt plan plus generated raster images when image generation is available.

This skill focuses on the illustration layer, not the full social-card layout. If the user also needs Xiaohongshu/WeChat card composition, pair this skill with the social-card skill: use this skill for the central illustrations, then place the generated images in the card template.

This skill does not replace the user's PPT skill or the Guizang social-card text-layout skill. Use those skills for slide structure and 3:4 text/card layout. Use this skill for the visual asset that goes into those layouts.

## Workflow

1. Read the user's source text, screenshot, or data and identify the concepts or charts that deserve supporting images.
2. Decide the working mode yourself from context. Do not ask the user to choose a mode unless the missing choice would materially change the result. If clarification is needed, ask naturally in one short sentence and offer a recommended default.
3. If the source is a chart screenshot, extract only chart type, title, data, axis labels, axis range, tick labels, units, category order, and error bars. Do not carry over screenshot colors, typography, spacing, shadows, or background.
4. If the concept, entity, object, historical/cultural context, scientific mechanism, species, material, brand, model, or place is likely unfamiliar or visually specific, look up reference information and/or reference images before prompting. Use references to understand and extract stable visual cues, then translate those cues into the Guizang visual system.
5. Compress each non-chart concept into one plain-language explanation and 3-5 visible diagram labels when labels help. Some editorial or humanities illustrations may need fewer labels.
6. Choose a visual structure for each concept:
   - Cycle: repeated work, feedback, loops, iteration.
   - Pipeline: ordered steps, routing, transformation, workflow.
   - Hub-and-spoke: one center coordinating several branches.
   - Before/after: state change, upgrade, migration, comparison.
   - Layer stack: architecture, hierarchy, dependencies.
   - Data-first scene: chart or metric panel embedded in a topical scene.
   - Scientific mechanism: object, parts, forces, reactions, or biological process.
   - Text scene: a literary, historical, or everyday scene that anchors an abstract idea.
7. Write one image prompt per illustration. Make the prompt describe exact label text or chart data, aspect ratio, safe margins, references used, and shared Guizang visual style.
8. Generate the images with the `imagegen` skill or built-in image generation tool when available.
9. Inspect each image. If labels are wrong, chart data is wrong, reference cues are misleading, unreadable, or clipped, regenerate with stricter constraints.
10. Save prompts and final image paths in the task folder so the image set can be reproduced.

## Text Rules

- Use everyday Chinese. Avoid literal field labels like `Trigger`, `Stop`, `Use` unless the user explicitly wants bilingual UI.
- Keep labels short: 2-5 Chinese characters is ideal; 6 characters is usually the upper limit.
- Put explanatory sentences outside the illustration when the final artifact has a surrounding layout. Inside the illustration, only label objects and flows.
- Prefer concrete labels over abstract nouns. Use `用户提示`, `AI 执行`, `结果检查`, `下一轮`; not `输入阶段`, `执行阶段`, `验证阶段`.
- If the model struggles with Chinese text, shorten labels further and use distinct positions: top-left, top-center, top-right, bottom-center.
- Do not add a dense legend inside the image. If a viewer needs a paragraph, it belongs in HTML/CSS, Markdown, or slide text.

## Visual System

Read `references/visual-style.md` before generating a new set. It defines the default 3D Swiss editorial style, aspect ratios, safe-area rules, and supported accent colors.

Read `references/prompt-patterns.md` when drafting prompts. It contains reusable prompt shells for cycle, pipeline, hub, before/after, and layer-stack diagrams.

Read `references/chart-beautify.md` when the input is a chart screenshot, table, metric list, benchmark result, or the user asks to beautify a chart. It covers how to preserve exact values while giving the chart the same material illustration style.

Read `references/use-cases-and-routing.md` when deciding what kind of image to create from vague user input, education materials, humanities topics, or mixed article/data sources.

Read `references/reference-gathering.md` when the topic contains unfamiliar concepts, specific entities, scientific objects, cultural artifacts, historical scenes, brand/model names, or anything where visual accuracy matters.

Read `references/qa-checklist.md` before delivering final images.

## File Handling

Create a task folder instead of writing loose assets next to this skill. Default to:

```text
local-tests/<slug>/
├── assets/
│   └── generated illustration images
└── PROMPTS.md
```

When pairing with another project, use that project's task folder if the user specifies one.

For each final image, record:

- Concept name.
- Final prompt.
- Output image path.
- Any rejected attempt and the reason if it matters for reuse.

## Quality Check

Before delivery, visually inspect each image and confirm:

- The whole diagram fits the requested image well; no important object or label is cropped.
- Chinese labels are legible and match the requested text.
- Label positions point to the right objects.
- Accent color is consistent across the set.
- The image contains no accidental logos, watermarks, UI chrome, or unrelated English text.
- The illustration can still be understood at social-card size.
