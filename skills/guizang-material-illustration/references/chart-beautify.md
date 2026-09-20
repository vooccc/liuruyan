# Chart Beautify

Use this reference when the user provides a chart screenshot, table, benchmark result, or metric list and wants a more polished Guizang-style chart.

## Principle

Let the image model handle visual material, depth, icons, and atmosphere. Do not let it infer the data.

When the input is a screenshot, extract the chart's semantic structure only. Do not inherit the screenshot's visual style.

Keep:

- Chart type.
- Title.
- Data values.
- Category order.
- X-axis and Y-axis labels.
- Axis range, tick labels, and units.
- Error bars or confidence ranges.

Discard:

- Source colors.
- Source typography.
- Source bar shape, line style, spacing, shadows, and background.
- Source screenshot crop and export artifacts.

After extraction, redraw the chart in the Guizang material illustration style.

Do not assume the source layout is worth preserving. If the source chart is plain, cramped, ugly, or the user only provides raw data, compose a new editorial data illustration from scratch.

Before prompting, extract or define:

- Chart title.
- Chart type: bar, line, scatter, ranking, matrix, etc.
- Axis range and tick labels.
- Category order.
- Exact values and value labels.
- Error bars or confidence ranges if present.
- Any rule such as "lower is better" or "higher is better".

If the exact data cannot be read from a screenshot, say so and either ask for the data or mark uncertain values as approximate.

## Composition Strategy

Choose one of two chart composition modes.

### Faithful Chart Redraw

Use this only when the original chart layout is already good and the user mainly wants polish.

- Keep the same chart type and approximate chart footprint.
- Preserve the original reading order and emphasis.
- Redraw the chart with Guizang materials, icons, and cleaner hierarchy.
- Do not inherit source colors, typography, shadows, or background.

### Data-First Editorial Composition

Use this by default when:

- The source chart layout is weak, generic, or too screenshot-like.
- The user only provides data.
- The chart topic has a clear scene or metaphor.
- The final image should feel more like a shareable data illustration than a report screenshot.

In this mode:

- Let the chart occupy roughly 45-70% of the image, not necessarily the whole canvas.
- Build a small scene around the chart: people, devices, product objects, workflow props, location cues, materials, or domain-specific objects.
- Treat the title as part of the scene when useful: terminal header, lab label, dashboard placard, shipping tag, field notebook, etc.
- Keep data marks readable and accurate. The scene must support the chart, not compete with it.
- Use asymmetry, depth, layers, and foreground/background props to avoid a plain chart screenshot.

Prompt pattern:

```text
Design a new composition from the extracted data: a compact [chart type] panel should occupy about [45-70%] of the image, while the rest is an illustrated [topic scene]. The title can be stylized as [scene object]. The image should feel like [metaphor], not a plain chart screenshot.
```

Examples:

- Coding benchmark: terminal testing lab with small operators, terminal screens, servers, cables, model icons.
- Travel spend chart: tabletop route map with luggage tags, receipts, tiny landmarks.
- Fitness progress chart: training mat, timer, weights, recovery icons around a compact chart panel.
- Finance chart: ledger desk, coins/cards, tiny risk flags, clean dashboard panel.

## Reference Lookup For Entity Icons

When chart categories contain concrete entities, enrich the chart with small semantic icons if they help the viewer scan the data.

Look up reference images when:

- The entity is a brand, model family, product, app, dataset, city, material, object, animal, food, or other visual concept.
- The model may not know the current logo, icon, object shape, or visual convention.
- Multiple categories come from different vendors or domains and would benefit from distinguishable icons.

Do not look up references when:

- All categories share the same visual identity and icons would add noise.
- The user asks for a purely abstract chart.
- The data is sensitive and visual branding could imply endorsement.

Reference workflow:

1. Search for 1-3 reference images per icon family, preferably official or widely recognized sources.
2. Extract only the stable visual cue: silhouette, symbol type, geometry, motif, or icon metaphor.
3. Prompt the image model to create a stylized 3D material icon based on that cue.
4. Keep all icons in the same Guizang material system: off-white / light gray base, one accent color, soft studio shadows.
5. Do not paste flat logos into the final chart unless the user explicitly asks for exact logos.

Prompt pattern:

```text
Reference-informed icons: include a small 3D icon beneath each category, styled in the same white/gray/IKB blue material system. [Entity group A] uses a simplified [visual cue]. [Entity group B] uses a simplified [visual cue]. These are stylized brand cues, not pasted flat logos.
```

Example:

```text
GPT/OpenAI categories use a simplified interlocking knot icon. Claude categories use a simplified radial starburst icon. Gemini category uses a simplified four-point sparkle icon. These should be stylized brand cues, not pasted flat logos.
```

## Prompt Requirements

For chart generation, include a `Required chart accuracy` block in the prompt. List every value explicitly.

Example:

```text
Required chart accuracy: y-axis tick labels must be exactly 1.0, 1.5, 2.0, 2.5, 3.0. Bar heights must match the values relative to this axis. Category labels must be exactly: "Sonnet 4.6", "Mythos Preview", "Opus 4.8", "Sonnet 5". Value labels must be exactly: "2.89", "1.95", "2.10", "2.53".
```

Also include:

- `Do not add extra categories.`
- `Do not swap bar order.`
- `Do not invent extra tick labels.`
- `All text must be crisp, readable, and spelled exactly as specified.`

## Visual Treatment

Use the same visual language as labeled material illustrations:

- Off-white background.
- Black ink axes and clean gridlines.
- Material bars or data marks with rounded edges and soft studio shadows.
- One Guizang accent system. Use source colors only when the colors encode meaning that is not otherwise represented.
- Small 3D icons may sit under category labels or beside row labels.
- Keep the chart recognizable as a chart, not a decorative poster.

Good chart enhancements:

- Material columns for bars.
- Tiny 3D icons representing each model, row, or category.
- Soft shadows that do not hide the coordinate system.
- Value labels above marks.
- Minimal callout for a highlighted point.

Avoid:

- Overly cinematic backgrounds.
- Perspective that distorts the axis.
- Decorative 3D objects that obscure bars, labels, or error bars.
- Extra explanatory text inside the plot area.
- Preserving a weak screenshot layout just because a screenshot was provided.

## QA Checklist

After generation, inspect the image and verify:

- Axis range and tick labels are correct.
- Category order is correct.
- Every value label is exact.
- Bar heights or data marks visually match the numbers.
- Error bars are present when requested and roughly match the specified ranges.
- Text has no spelling mistakes.
- No source watermark or fake logo was added.

If any number, category, or tick label is wrong, regenerate. Do not accept an attractive chart with wrong data.

## Example Prompt: Benchmark Bar Chart

```text
Use case: infographic-diagram
Asset type: 16:9 polished benchmark chart illustration
Primary request: Create a polished material-style bar chart titled "Misaligned behavior". The chart must show four model scores on a y-axis from 1.0 to 3.0. Lower score is better. Preserve these exact data values and print the value above each bar: Sonnet 4.6 = 2.89, Mythos Preview = 1.95, Opus 4.8 = 2.10, Sonnet 5 = 2.53. Use vertical bars with subtle 3D material depth, soft studio shadows, rounded top edges, and small 3D model icons beneath each category.
Required chart accuracy: y-axis tick labels must be exactly 1.0, 1.5, 2.0, 2.5, 3.0. Bar heights must match the values relative to this axis. Category labels must be exactly: "Sonnet 4.6", "Mythos Preview", "Opus 4.8", "Sonnet 5". Value labels must be exactly: "2.89", "1.95", "2.10", "2.53".
Style/medium: clean Swiss editorial 3D vector-like chart, off-white background, black ink axis lines, refined gray surfaces, material columns, soft contact shadows, one vivid IKB blue accent (#002FA7), and restrained Guizang material colors.
Composition/framing: wide 16:9 chart, generous margins, no cropped labels, title centered at top, axis label on the left, clean gridlines.
Text constraints: all text must be crisp, readable, and spelled exactly as specified. Do not add extra words beyond the title, axis label, tick labels, category labels, and value labels.
Avoid: wrong numbers, wrong tick labels, extra categories, swapped bar order, distorted coordinate system, watermark, logo, decorative blobs, gradient background.
```
