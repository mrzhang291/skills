# Pending 与飞书入档

用于评分草稿保存、HR 确认、飞书入档、附件上传和失败处理。

## Pending 生命周期

评分完成后先保存 pending，再给 HR 看。

如果 HR 上传的是 `.doc` / `.docx`，评分前必须先运行：

```bash
python scripts/prepare_resume_file.py --file "<HR上传文件路径>"
```

然后把脚本返回的 `pending_fields` 合并进评分 JSON：`简历文件路径` 使用转换后的 PDF，`原始简历文件路径` 保留 HR 上传的 Word 原文件。这样后续入档附件会上传 PDF，阅读范围和飞书附件保持一致。

```bash
python scripts/pending_store.py save \
  --position-id <position_id> \
  --position-name "<岗位名>" \
  --candidate "<候选人标识>" \
  --record <评分JSON路径>
```

HR 要求修改时更新 pending：

```bash
python scripts/pending_store.py update \
  --pending <pending_id或pending_file> \
  --hr-feedback "HR反馈" \
  --tier borderline \
  --score 65 \
  --status needs_revision
```

HR 明确确认入档时：

```bash
python scripts/pending_store.py confirm \
  --pending <pending_id或pending_file> \
  --hr-feedback "HR原话"
```

`confirm` 返回的 `record_file` 作为入档 JSON。
该路径是相对 recruiter skill 根目录的路径；`append-record` 支持直接传这个相对路径，也支持传绝对路径。

## 入档命令

```bash
python scripts/feishu_jd.py append-record \
  --position <岗位名或position_id> \
  --record <pending JSON路径>
```

默认查重：同岗位 table 中相同 `候选人Key` 会跳过新建并返回已有 record_id；旧表会兼容检查 `候选人标识` / `候选人`。

需要保留多次评分：

```bash
python scripts/feishu_jd.py append-record \
  --position <岗位名或position_id> \
  --record <pending JSON路径> \
  --duplicate-action create
```

需要修正已入档的同候选人记录：

```bash
python scripts/feishu_jd.py append-record \
  --position <岗位名或position_id> \
  --record <pending JSON路径> \
  --duplicate-action update
```

## 入档验证（只读）

需要确认某条候选人是否已经写入飞书时，使用脚本内置只读验证命令：

```bash
python scripts/feishu_jd.py verify-archive \
  --position <岗位名或position_id> \
  --candidate "<候选人姓名、pending_id、候选人Key或record_id>"
```

如果不传 `--candidate`，会返回该岗位入档表的记录摘要；如果不传 `--position`，会按 `position_id_map` 验证所有已配置岗位。该命令只读取飞书记录，不会创建 table、不会补字段、不会写入记录。

排查时不要手写临时 API 脚本直接读取 `config.json.feishu.app_secret`。配置里通常是 `${FEISHU_APP_SECRET}` 占位符，正确流程是始终通过 `feishu_jd.py` 的子命令，让脚本统一解析环境变量、获取 token 和输出 JSON。

## 入档结构

所有岗位共用一个招聘入档 Base，每个 `position_id` 一个 table。

`config.json`:

```json
{
  "archive_base": {
    "app_token": "...",
    "url": "https://...",
    "created_at": "YYYY-MM-DD"
  },
  "archive_tables": {
    "fde-intern": {
      "table_id": "...",
      "created_at": "YYYY-MM-DD"
    }
  }
}
```

首次入档某岗位时，脚本会自动创建或复用 table，并写回 `archive_tables`。

## 字段

紧凑版入档字段：

- 候选人。
- 初筛结论。
- 分数。
- 建议动作。
- HR反馈。
- 评审摘要。
- 简历附件。
- 入档时间。
- 岗位。
- position_id。
- tier。
- pending_id。
- 候选人Key。

`评审摘要` 会合并推荐理由、匹配亮点、风险点、不确定项、阅读范围、评分依据和简历文件名，避免把飞书表格拆成过多长文本列。

脚本写入字段与上方 13 个字段使用同一套标准字段清单。`append-record` 写入前会校验目标飞书 table 的字段集合必须完全一致；如有缺失字段或额外字段，会停止写入并返回 schema mismatch，不会回退写旧字段。

已有旧表可以尝试补齐缺失的紧凑版字段：

```bash
python scripts/feishu_jd.py sync-archive-schema --position <岗位名或position_id>
```

或批量补齐：

```bash
python scripts/feishu_jd.py sync-archive-schema --all
```

补齐字段不会删除旧列，避免误删历史数据。注意：如果旧表仍保留额外字段，隐藏视图列并不等于删除字段，`append-record` 仍会因为 `extra_fields` 停止写入。要继续入档，需要使用只有 13 个标准字段的干净 table，或由管理员明确清理额外字段后再写入。

## 附件

评分 JSON 中如有 `简历附件_file_token`，直接写入附件字段。

如有 `简历文件路径` / `简历本地路径` / `resume_path` / `file_path`，脚本会先上传附件。单个附件默认上限 20MB。

Word 简历的附件规则：

- `简历文件路径` 必须指向 `prepare_resume_file.py` 转出的 PDF。
- `原始简历文件路径` 可以保留 Word 原路径，只作追溯，不用于飞书附件上传。
- 转 PDF 失败时不要入档 Word 原件；先让 HR 补 PDF，避免飞书附件和实际阅读材料不一致。

附件上传失败不阻断记录创建：附件字段留空，文本字段照写，并把失败原因告诉 HR。

## 凭证

`config.json` 不写明文 App Secret。脚本优先读取环境变量：

```powershell
$env:FEISHU_APP_SECRET = "..."
```

凭证失败时，提示检查 `app_id` 和 `FEISHU_APP_SECRET`。

## 失败处理

- JD 目录字段不匹配：按脚本返回的 `available_fields` 更新 `config.json`。
- JD 目录只想读取某个视图：在 `config.json.feishu.jd_index_view_id` 填入 URL 里的 `view=` 参数；留空则读取整张表。
- JD 文档链接支持 `/docx/` 和 `/wiki/` 链接；如果使用 wiki 链接，飞书应用也需要 wiki 只读权限。
- 入档 table 创建失败：告诉 HR 入档失败但本地记录已保存；检查飞书应用权限后重试。
- 查重失败：不要强行写入；先排查飞书权限或网络。
- `position_id_map` 无岗位：先让 HR 确认岗位映射。
