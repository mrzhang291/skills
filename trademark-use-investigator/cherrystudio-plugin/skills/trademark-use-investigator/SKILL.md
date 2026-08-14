---
name: trademark-use-investigator
description: 在 CherryStudio 中调查商标近三年实际使用、撤三线索、企查查图样、淘宝京东1688及百度360公开搜索，并生成含 HTML、时间戳和可点击来源网址的 PDF 时必须使用。必须先启动专用 Edge（无 Edge 时 Chrome）Profile；禁止使用 WebSearch、WebFetch 或 Cherry 内置 web_search 代替官方流程。
---

# CherryStudio 商标调查启动守卫

这是随本地 Plugin 加载的强制入口，不是另一套调查流程。

1. 立即定位并完整读取主 Skill，依次检查：`${CLAUDE_PROJECT_DIR}/.claude/skills/trademark-use-investigator/SKILL.md`、`${CLAUDE_PLUGIN_ROOT}/../../skills/trademark-use-investigator/SKILL.md`、`%APPDATA%/CherryStudioEnterprise/Data/Skills/trademark-use-investigator/SKILL.md`。只能选择真实存在且 `name: trademark-use-investigator` 的文件；三处都不存在时报告 `skill_installation_incomplete` 并停止，禁止改用网页搜索。
2. 严格按其中“不可跳过的启动契约”和“基础环境与固定六站点硬门”执行。
3. 第一项网络或浏览器动作只能是官方 `cherrystudio_orchestrator.py start`；必须由它自动打开同一专用 Profile 中的企查查、淘宝、京东、1688、百度和 360 搜索，且优先 Edge、仅无 Edge 时回退 Chrome。
4. 禁止调用 `WebSearch`、`WebFetch`、`mcp__cherry-tools__web_search`、`mcp__cherry-tools__web_fetch`、Exa、通用浏览器搜索、curl 或临时抓取脚本。
5. 未弹出专用 Profile、六个必需站点缺失、官方入口非零或机器状态不满足验收条件时，原样报告机器状态并停止，不得自行换方法。
