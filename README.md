# Skills 技能库

个人收集与维护的 AI Agent Skills，适用于 CherryStudio、Claude Code 等支持 Skill 的工具。

## 已有技能

### 🔍 trademark-use-investigator · 商标实际使用调查

面向撤三（三年不使用撤销）场景的商标实际使用线索调查与证据固证：

- **六站点覆盖** — 企查查图样参考、淘宝 / 京东 / 1688 站内搜索、百度 / 360 公开搜索
- **受控自动化** — 专用浏览器 Profile，真人只需登录和过验证码，其余全自动
- **证据级固证** — 截图 / HTML / MHTML / PDF 全链路，逐页带来源 URL、北京时间与 SHA-256
- **可审计交付** — 唯一交付 PDF 嵌入原始 HTML，来源链接与目录均可点击

详见 [`trademark-use-investigator/SKILL.md`](trademark-use-investigator/SKILL.md)。

### 🧑‍💼 recruiter · 招聘初筛助手

面向 HR 的简历 / 作品集初筛 skill：

- **飞书 JD 唯一数据源** — 每次评分实时读取飞书多维表格里的岗位 JD，不用本地缓存
- **三档判断** — 输出 pass / borderline / reject + 分数，先存本地 pending，HR 明确确认后才写入飞书入档表
- **视觉读作品集** — Word 先转 PDF，支持多模态逐页阅读设计师作品集，产出证据化笔记
- **反馈学习** — HR 反馈沉淀为示例库与岗位偏好，多岗位按 position_id 严格隔离

详见 [`recruiter/SKILL.md`](recruiter/SKILL.md)。

## 使用方式

将对应技能目录放入工具的 Skill 目录（如 `.claude/skills/`）并激活即可，具体安装与使用步骤见各技能自己的 `SKILL.md`。

## 目录约定

每个技能独立一层目录，仓库不提交依赖与缓存（`node_modules`、`__pycache__` 等已由 `.gitignore` 排除）：

```text
skills/
├── trademark-use-investigator/   # 商标使用调查
├── recruiter/                    # 招聘初筛助手
└── <下一个技能>/                  # 敬请期待
```