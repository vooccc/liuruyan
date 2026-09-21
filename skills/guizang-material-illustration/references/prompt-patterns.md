# Prompt Patterns

Use these patterns after reading `visual-style.md`. Replace bracketed fields; keep the final prompt direct and concrete.

## Base Prompt Shell

```text
Use case: stylized-concept
Asset type: [wide horizontal 1.9:1 / 16:9 / 1:1] labeled material illustration for [final use]
Primary request: [plain English description of the concept and visual metaphor].
Chinese labels: Add [3-5] short Simplified Chinese labels as clean printed callouts inside the illustration: "[label 1]", "[label 2]", "[label 3]", "[label 4]". Place each label near the matching object; keep labels horizontal, large, high-contrast, readable, and away from edges.
Style/medium: clean Swiss editorial 3D vector-like illustration, off-white background, black ink lines, refined gray surfaces, one vivid [accent name] accent ([hex]).
Composition/framing: [ratio] composition, subject fills the width naturally, centered vertically, generous safe margins on all sides, full subject visible, no crop, designed for [known image well if any].
Lighting/mood: crisp studio light, calm analytical mood.
Constraints: no extra words beyond the specified Chinese labels, no English labels, no numbers unless requested, no logo, no watermark, no poster frame, no page title, no decorative blobs, no gradient background.
```

## Cycle Diagram

Use for loops, feedback, repeated work, iteration, recurring behavior.

```text
Primary request: [Concept] shown as a simple cycle. Arrange [object A], [object B], [object C], and [object D] around a circular arrow path so the viewer sees how work repeats until it returns to the start.
Chinese labels: Add four short Simplified Chinese labels as clean printed callouts inside the illustration: "[start label]", "[work label]", "[check label]", "[return label]".
```

Example labels: `用户提示`, `AI 执行`, `结果检查`, `下一轮`.

## Pipeline Diagram

Use for step-by-step work, transformation, routing, review, lifecycle.

```text
Primary request: [Concept] shown as a left-to-right pipeline. A clear input object enters on the left, passes through [step 1 object], [step 2 object], and [step 3 object], then exits as a completed result on the right.
Chinese labels: Add four short Simplified Chinese labels as clean printed callouts inside the illustration: "[input]", "[step 1]", "[step 2]", "[output]".
```

Example labels: `事件进入`, `工作流分流`, `子代理处理`, `复查收口`.

## Goal And Evaluator Diagram

Use for goals, success criteria, judging, retry loops.

```text
Primary request: Goal-based agent loop illustration. A clear target or finish condition sits on the left, an AI builder iterates through small task blocks in the middle, and a neutral evaluator gate checks whether the target is met on the right. Add a subtle return arrow from the evaluator back to the AI builder for failed attempts.
Chinese labels: Add four short Simplified Chinese labels as clean printed callouts inside the illustration: "明确目标", "AI 尝试", "评估器", "未过重试".
```

## Time Check Diagram

Use for scheduled checks, polling, recurring summaries, CI/PR monitoring.

```text
Primary request: Time-based agent loop illustration. A clock and calendar on the left trigger a small AI inspector in the center to periodically check an external system on the right, represented by abstract cards, status dots, and a small queue. Add a return line that waits for the next scheduled check.
Chinese labels: Add four short Simplified Chinese labels as clean printed callouts inside the illustration: "时间触发", "AI 巡检", "外部系统", "等下次".
```

## Hub-And-Spoke Diagram

Use for orchestration, multiple agents, classification, parallel work.

```text
Primary request: [Concept] shown as a central routing hub. Incoming items arrive from the left, the hub splits them into several parallel branches, each branch has a small agent working on a task, and the branches merge into a reviewed output on the right.
Chinese labels: Add four short Simplified Chinese labels as clean printed callouts inside the illustration: "[input]", "[hub]", "[branch work]", "[review/output]".
```

## Before/After Diagram

Use for transformation, migration, upgrade, cleanup, contrast.

```text
Primary request: [Concept] shown as a before-and-after transformation. The left side shows [old state] as messy or unresolved, the center shows [intervention/process], and the right side shows [new state] as organized and complete.
Chinese labels: Add three short Simplified Chinese labels as clean printed callouts inside the illustration: "[before]", "[process]", "[after]".
```

## Label Repair Prompt

If the image is visually good but labels are wrong, regenerate. Do not try to patch text with HTML unless the user asks for external overlay text.

```text
Regenerate the same illustration with the same composition and style, but make the Chinese labels exactly: "[label 1]", "[label 2]", "[label 3]", "[label 4]". Use large printed callouts on quiet white plates, horizontal text, high contrast, and place them away from all edges. No other text.
```
