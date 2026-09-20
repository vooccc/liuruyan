# Handoff — guizang-material-illustration

最后更新：2026-07-07 · 版本：v0.1

这份文档只记录当前 Skill 的事实：文件结构、分工、验证方式、测试案例和已知坑。产品定位看 `PRODUCT.md`。

远端仓库：`https://github.com/op7418/guizang-material-illustration.git`

---

## 1. 目录结构

```
guizang-material-illustration/
├── SKILL.md                         # Skill 入口：何时调用、工作流、交付规则
├── HANDOFF.md                       # 本文件
├── PRODUCT.md                       # 产品文档
├── agents/
│   └── openai.yaml                  # Codex / OpenAI Skill 展示配置
├── assets/
│   └── prompt-template.md           # 可复用提示词模板
└── references/
    ├── visual-style.md              # 归藏材质插画风格、比例、安全区、图内文字规则
    ├── prompt-patterns.md           # 常用图解结构：循环、管线、对比、分层、场景
    ├── chart-beautify.md            # 图表语义抽取、数据优先重画、图标参考
    ├── use-cases-and-routing.md     # 支持场景和内部路由
    ├── reference-gathering.md       # 生僻概念 / 品牌 / 科学装置的参考信息规则
    └── qa-checklist.md              # 图内文字、数据、裁切、参考准确性检查
```

---

## 2. Skill 分工

这个 Skill 只做**配图层**：带中文标签的解释图、概念图、机制图、教育图、人文配图、工作场景图、材质化图表和参考信息辅助图。

它不负责完整社交卡片、PPT 页面或公众号封面排版。需要外层排版时，把它生成的图片交给社交媒体卡片 Skill 或 PPT Skill。

关键约定：

- 图像本身可以有字。只要这张图承担解释任务，图内短标签就是内容的一部分。
- 不把图解降级成无字装饰图。
- 不把提示词、Skill 名称、内部模式、制作过程写进给观众看的成品。
- 模式由 Agent 自己判断，用户不需要先选「图表模式 / 教育模式 / 人文模式」。

---

## 3. 工作流事实

1. 读用户材料，拆出要解释的核心关系、对象、流程、数据或情绪。
2. 内部判断图型：概念图、流程图、图表、人文场景、教育机制、工作流、参考信息辅助图等。
3. 如果出现生僻概念、品牌、模型、科学装置、历史文化物件或专业图标，先搜参考信息和参考图，只提取事实与稳定视觉线索。
4. 图表输入只保留语义：图表类型、标题、数据、坐标轴、单位、类别顺序、误差线、重要标注；不复刻糟糕截图的布局。
5. 写图像提示词，把图内中文标签、数值、构图安全区和风格限制写清楚。
6. 用 GPT-Image / imagegen 生成图片。
7. 检查文字、数据、裁切、主体大小、图例和标签；错了优先重新生成，不靠 HTML 硬贴一堆标签补救。
8. 交付图片路径、必要时交付对应提示词记录。

---

## 4. 测试案例

本轮已经做过三组测试。测试产物属于本地临时文件，不随 Skill 分发；这里保留案例类型，方便后续重建 gallery。

### 教育 / 人文 / 图表混合测试

- 小学物理杠杆：`支点 / 用力点 / 阻力点 / 力臂`
- 中学物理电磁感应：`磁铁 / 线圈 / 运动方向 / 感应电流 / 小灯泡`
- 丝绸之路文化交流：`长安 / 商队 / 绿洲 / 交流`
- 古诗月光与思乡：`月光 / 床前 / 远望 / 思乡`
- 科学展甘特图、一天时间桑基图、酶活性热力图

### 打工人和内容生产者场景

- 周报汇报、项目管理、运营指标
- Andon 异常升级、PKCE 产品说明
- Zettelkasten 内容生产、Kirkpatrick 培训评估
- Panopticon 职场隐喻、累计流图

### 配图 Skill × 社交卡片 Skill 联动

- IKB Blue：AI 协作
- Lemon Yellow：内容生产
- Lemon Green：专注管理
- Safety Orange：风险分流

---

## 5. 已知坑

- 3:4 社交卡片里如果主角是配图，图片区域必须足够大；小图缩放后，图内标签和细节会读不清。
- 不要在图像已经生成后用 HTML 贴一堆解释字来补救。图内标签错了或缺了，优先重生图。
- 不要让用户硬选模式。除非上下文真的不足，否则直接基于材料判断。
- 参考搜索不是找风格。只能提取事实、结构、图标和视觉线索，再统一转成归藏材质插画。
- 科学教育图不能只好看，方向、关系、部件标签要对。
- 图表不能只追求漂亮，数据和坐标含义必须先对。

---

## 6. 验证

Skill 结构验证命令：

```bash
python3 /Users/guohao/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/guohao/Documents/code/HyperFrames-test/guizang-material-illustration
```

期望输出：

```text
Skill is valid!
```
