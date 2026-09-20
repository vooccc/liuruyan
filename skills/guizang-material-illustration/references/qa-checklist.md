# QA Checklist

Run this before delivering final images.

## Universal

- The image answers the user's actual content need, not merely the production method.
- The composition improves weak source screenshots into a clear, readable visual.
- The whole subject fits; no important text, label, icon, object, or axis is cropped.
- Text is readable at intended display size.
- No accidental logos, watermarks, extra English, or misspelled Chinese.
- The style is consistent with the Guizang material / Swiss editorial visual system.
- Negative constraints in the prompt protect the main risk: wrong data, clutter, wrong tone, unsupported facts, or cropped labels.

## Labels And Explainers

- Labels are short and attached to the correct objects.
- The viewer can understand the main relationship without reading a paragraph.
- The image does not contain a dense in-image legend.
- If labels are wrong or garbled, regenerate rather than patching with unrelated overlay text unless the user wants external layout text.

## Charts

- Chart type matches the data.
- Axis range and tick labels are correct.
- Category order is correct.
- Every value label is exact.
- Data marks visually match the numbers.
- Error bars or uncertainty ranges appear when requested.
- Scene elements do not block chart reading.
- If a chart is attractive but numerically wrong, reject it.

## Reference-Informed Images

- Reference cues are accurate enough for the intended audience.
- Brand/model/entity icons are stylized into the shared material system.
- The image does not paste flat logos unless explicitly requested.
- Historical, cultural, scientific, or biological details do not imply unsupported certainty.

## Education And Science

- Labels point to the correct part or process.
- Mechanism, scale, direction, or sequence is not misleading.
- Cute scene elements do not distract from the concept.
- Uncertain details are noted in `PROMPTS.md`.
