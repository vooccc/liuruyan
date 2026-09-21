# chart-generation Capability

Provides: chart-generation
Skill: chart-image

## Methods

### lineChart

**Input:**
- data: array of chart points
- title: optional chart title
- groupBy: optional field for multi-series charts
- options: optional rendering flags such as `dark`, `focusRecent`, `showChange`, `showValues`

**How to fulfill:**
- Invoke `scripts/chart.mjs` with structured CLI arguments, not by interpolating untrusted text into a shell command.
- Pass chart data through a JSON argument or a trusted temporary file created by the runtime/tooling layer.
- Map supported options to the corresponding chart flags (`--dark`, `--focus-recent`, `--show-change`, `--show-values`).
- Return the generated chart image through the caller's normal file/attachment flow.

### barChart

**Input:**
- data: array of objects with label/value-style fields
- title: optional chart title
- options: optional rendering flags such as `dark` and `showValues`

**How to fulfill:**
- Invoke `scripts/chart.mjs` with structured CLI arguments, not shell-built strings.
- Provide the data as JSON or via a trusted temporary file.
- Map supported options to the corresponding chart flags.
- Return the generated chart image through the caller's normal file/attachment flow.

### areaChart

**Input:**
- data: array of chart points
- title: optional chart title
- options: optional rendering flags similar to `lineChart`

**How to fulfill:**
- Invoke `scripts/chart.mjs` with structured CLI arguments.
- Provide the data as JSON or via a trusted temporary file.
- Map supported options to the corresponding chart flags.
- Return the generated chart image through the caller's normal file/attachment flow.

## Safety notes

- Do not build shell commands with string interpolation around user-controlled JSON, titles, labels, or paths.
- Prefer argv-style process execution over `sh -c`, command substitution, or inline `echo '...json...'` patterns.
- Treat `--output`, `--spec`, and file-based inputs as trusted/runtime-controlled parameters rather than free-form user text.
- Use platform-native attachment/message tools to deliver the image after generation.
