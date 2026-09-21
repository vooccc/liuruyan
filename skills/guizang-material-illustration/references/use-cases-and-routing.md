# Use Cases And Routing

Use this reference to decide what kind of supporting image to create. The user should not need to pick a mode. Infer the best mode from the input and the intended use.

## Boundary

This skill generates supporting images. It does not own:

- Full PPT structure or slide systems. Use the user's PPT skill for that.
- Full Xiaohongshu / WeChat 3:4 text layout. Use the Guizang social-card skill for that.
- Exact editable vector diagrams or engineering CAD.
- Pure logo design or brand identity systems.

It can generate the visual asset that those other skills place into a final layout.

## Decision Logic

Pick the mode internally:

- If the input is an article or long note, pick 1-6 "cognitive anchors" instead of illustrating every paragraph.
- If the input is a chart, table, or metric list, use chart beautify or data-first editorial composition.
- If the input is a process, use cycle, pipeline, or hub-and-spoke.
- If the input is a scientific mechanism, show the object/process directly with labels and simple spatial logic.
- If the input is a literary, historical, social, or philosophical idea, build a scene or symbolic arrangement that clarifies the idea without over-explaining it.
- If the input is vague but usable, proceed with the most likely mode and keep the prompt record. Ask only when the missing choice changes the image materially.

## Supported Image Types

### Workplace / Knowledge Work

Use this skill for everyday work visuals when the user needs an image that makes a report, note, or deck easier to understand:

- Weekly/monthly reports: progress, blockers, wins, risks, next steps.
- Project management: roadmap, milestones, dependencies, ownership, RACI, handoff, review flow.
- Meeting outputs: decision trees, action-item maps, issue follow-up, before/after alignment.
- Strategy and planning: goal breakdown, OKR maps, priority matrices, trade-off diagrams.
- Operations: funnel, ticket flow, SLA, incident timeline, escalation path, support load, process bottlenecks.
- Sales / customer success: customer journey, pipeline stages, objections, renewal risks, account maps.
- HR / recruiting / training: onboarding path, role expectations, interview funnel, competency model, learning path.
- Finance / business: budget split, cost structure, unit economics, waterfall changes, scenario comparison.

### Content Creator / Editorial

Use this skill when a writer, creator, educator, or marketer needs article/post visuals:

- Article header or section image that anchors one key idea.
- Explainer image for a concept, framework, method, habit, or mental model.
- Comparison image: old vs new, A vs B, common mistake vs better approach.
- Checklist/process image: creator workflow, publishing pipeline, research method, editing loop.
- Social commentary or humanities idea: symbolic scene, relationship map, timeline, motif board.
- Product education: feature mechanism, user journey, use-case scene, value chain.
- Trend/data interpretation: small chart embedded in a topical scene.
- Newsletter/blog recurring columns: same visual system with different weekly topics.

For content creators, prefer one strong cognitive anchor over illustrating every sentence.

### Internet / Tech / Product

- Agent loops and workflows.
- Product feature diagrams.
- SaaS / devtool / AI model comparisons.
- Architecture or system diagrams.
- Benchmark charts and data scenes.
- Bug triage, CI, PR review, migration, dependency updates.

### Data And Charts

- Bar, line, area, ranking, scatter, bubble, matrix, funnel, timeline, and comparison charts.
- Gantt, roadmap, Kanban summary, calendar heatmap, milestone map, dependency map.
- Sankey, waterfall, treemap, radar, cohort, retention curve, burn-up/burn-down, box plot.
- Priority matrix, quadrant chart, decision tree, org chart, flowchart, swimlane, RACI map.
- Data-first editorial scenes where the chart is embedded in a lab, desk, map, dashboard, or tabletop environment.
- Multi-entity comparisons with reference-informed icons.
- Raw data turned into a shareable benchmark visual.

### Education

- 小学课文配图: scene images that help understand characters, setting, conflict, mood, or everyday objects.
- Biology: cells, organs, ecosystems, life cycles, food chains, genetics basics, anatomy, microscope scenes.
- Chemistry: molecules, reactions, lab setups, pH, mixtures, phase changes, periodic-table ideas.
- Physics: force, energy, circuits, optics, waves, motion, pressure, heat transfer.
- Geography: landforms, climate, maps, water cycles, city/region comparisons.
- Math: geometric relationships, fractions, probability, number lines, word-problem scenes.

### Humanities

Support humanities when the image can clarify a concept, scene, relationship, or contrast:

- History: timelines, trade routes, social structures, artifact-centered scenes, cause/effect chains.
- Literature: scenes, motifs, character relationships, narrative turning points, mood boards.
- Philosophy: conceptual contrasts, argument structure, thought experiments, schools of thought.
- Sociology / anthropology: social roles, rituals, institutions, networks, urban/rural contrasts.
- Art / design history: composition principles, movement comparisons, museum/tabletop artifact scenes.

Limits for humanities:

- Do not fabricate a "real" portrait, artifact, or event detail when accuracy matters.
- Use reference information for historical clothing, tools, maps, architecture, and cultural symbols.
- Prefer "inspired symbolic scene" over pretending to reproduce a historical photograph.

### Science And Technical Accuracy

For science, accuracy is more important than decoration:

- Search reference information when the mechanism, species, apparatus, or formula is not obvious.
- Keep labels short and placed on the correct parts.
- Avoid unnecessary cute characters if they distract from the mechanism.
- State uncertain details in the prompt record.

## Output Patterns

- Single supporting image for one concept.
- Two or three illustration variants for the same concept.
- Shot list only: describe image candidates without generating.
- Article image set: pick cognitive anchors and generate several images.
- Chart redesign: turn raw data or a screenshot into an editorial data visual.
- Reference-informed icon set inside a chart or diagram.

## Negative Constraints

Use negative constraints to protect clarity and accuracy:

- Do not turn every sentence into a separate label.
- Do not fill the image with paragraph text, legends, or UI-like panels unless the user asks for a dashboard.
- Do not preserve cramped screenshot layouts when a clearer editorial composition is possible.
- Do not let decorative props block labels, axes, values, arrows, or the main mechanism.
- Do not invent facts, scores, dates, citations, historical details, scientific parts, or brand marks.
- Do not use cute characters when the topic needs a serious work, science, finance, or policy tone.
- Do not make charts numerically approximate when exact values are provided.
