# Quick CLI 初筛

Quick 是显式选择的非穷尽性公开网络初筛，不属于 CherryStudio 生产入口。CherryStudio 不得分步执行本文命令。

## 初始化

1. 对同一 Skill 版本和运行环境预检一次：

~~~text
python "<SKILL_DIR>/scripts/preflight.py" --skill-dir "<SKILL_DIR>" --run-root "<WORKSPACE_DIR>/trademark-evidence" --profile full --output "<WORKSPACE_DIR>/trademark-preflight.json"
~~~

2. 使用 UTF-8 JSON 初始化；字段为 `profile/workspace/run_id/reference_file/trademark_name/registration_number/owner/goods/period`：

~~~text
python "<SKILL_DIR>/scripts/init-run-from-json.py" --intake-json "<INTAKE_JSON>"
~~~

3. 只分析参考文件中识别商标、注册号、权利人和商品所需的页面，把结果写入 `reference/reference.json` 并设置 `analysis_status=complete`。随后取得企查查精确详情和原始图样；不得把整份 PDF 放入模型上下文。

## 发现和筛选

4. 生成最多七个合并查询：

~~~text
python "<SKILL_DIR>/scripts/build-query-plan.py" --run-dir "<RUN_DIR>"
~~~

5. 有结构化搜索 JSON 时原样保存到 `discovery/raw/` 并逐查询导入：

~~~text
python "<SKILL_DIR>/scripts/import-discovery-results.py" --run-dir "<RUN_DIR>" --query-id Q001 --query "<QUERY>" --provider "<PROVIDER>" --source-kind "<SOURCE_KIND>" --raw-file "<RAW_JSON>" --allow-zero-results
~~~

没有可导出的结构化结果时运行有界 headless 发现器：

~~~text
python "<SKILL_DIR>/scripts/quick-discover.py" --run-dir "<RUN_DIR>"
~~~

百度跳转链接必须解析为最终站外 URL。frontier 达到至少八个直达 URL、两个站点且能选出至少六个探测候选时提前停止；单个提供方阻断不重试。

6. 本地排序，不联网、不打开浏览器：

~~~text
python "<SKILL_DIR>/scripts/rank-frontier.py" --run-dir "<RUN_DIR>" --limit 8
~~~

只考虑命中商标、注册号或权利人之一的候选；最多三个站点、每站三个候选。

7. 默认探测六个候选，有效页不足五个时最多补探到八个。探测只保存正文、元数据和小截图：

~~~text
python "<SKILL_DIR>/scripts/quick-probe.py" --run-dir "<RUN_DIR>"
~~~

验证码、登录、拒绝、超时或内容不符时立即跳过且不重试；原 URL 和状态保留，但不进入 PDF。

8. 只对 `probe-shortlist.json` 中有效页面做完整归档。目标五页，最多尝试六个 URL：

~~~text
python "<SKILL_DIR>/scripts/quick-capture.py" --run-dir "<RUN_DIR>"
~~~

完整归档保存 SingleFile HTML、MHTML、整页截图、PDF、元数据和哈希。

## 复核与完成

9. 生成紧凑复核包：

~~~text
python "<SKILL_DIR>/scripts/build-review-packet.py" --run-dir "<RUN_DIR>" --max-chars-per-candidate 3000
~~~

模型只读取复核 JSON 和必要元数据，不加载整份 HTML/MHTML/正文。

10. 用 `retain-visual-mark-matches.py` 做本地多尺度筛选。它只决定保留，不决定正面证据；拟作为正面证据的候选仍需两个不同模型家族一致复核。

11. 逐页提升四至五个内容正确且有解释力的页面。搜索页、阻断页、错误主体和离线失败页不得提升：

~~~text
python "<SKILL_DIR>/scripts/promote-candidate.py" --run-dir "<RUN_DIR>" --candidate-id C001 --source-id S001 --order 1 --label "<LABEL>" --page-role supporting
~~~

12. `results.json` 必须写明：

~~~json
{"investigation_scope":"quick_non_exhaustive","conclusion":"<限定范围结论>","limitations":["Quick 快速公开网络筛查，不是穷尽性调查。"]}
~~~

然后完成：

~~~text
python "<SKILL_DIR>/scripts/finalize-run.py" --run-dir "<RUN_DIR>" --skill-dir "<SKILL_DIR>"
~~~

只有 `validation.json.ok=true` 才能声明 Quick 完成。
