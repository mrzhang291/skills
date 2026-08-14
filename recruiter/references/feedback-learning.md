# HR 反馈学习

用于评分后处理 HR 反馈、写 examples、更新 profile、维护 history。

## 反馈分类

| HR 反馈 | 动作 |
|---|---|
| 同意 / 对 / 嗯 / OK | 记录正样本；不写飞书；询问是否确认入档 |
| 应该是 X 分 / 我给 X 分 / 应该 pass / 应该 reject | 记录修正样本；若缺理由，追问一句 |
| 不对，因为... / 判断错了 | 记录负样本 + 修正理由；抽取偏好 |
| 确认入档 / 写入飞书 / 归档这条 | 走 pending confirm + append-record |

## 偏好写入规则

- 跨岗位通用：写 `profiles/global.md`。
- 岗位特有：写 `profiles/positions/{position_id}.md`。
- 不确定作用范围：问 HR。
- 新抽取偏好默认 `pending`，写入 `待确认偏好`，不得参与评分。
- HR 明确说“以后都按这个/确认这条偏好”时，才 `confirmed` 并写入正式区。

偏好段落：

- `hard`: 硬性要求。
- `soft`: 软性偏好。
- `veto`: 否决项。
- `compensation`: 补偿规则。
- `note`: 备注。

## 落盘命令

记录反馈和示例：

```bash
python scripts/feedback_store.py add-feedback \
  --position-id <position_id> \
  --position-name "<岗位名>" \
  --candidate "<候选人标识>" \
  --tier pass \
  --score 73 \
  --hr-feedback "HR原话" \
  --key-experience "关键经验" \
  --core-skills "核心技能" \
  --concerns "主要风险" \
  --preference-scope position \
  --preference-section soft \
  --preference-status pending \
  --preference-note "抽取出的偏好"
```

没有新偏好时：

```bash
python scripts/feedback_store.py add-feedback ... --preference-scope none
```

确认偏好时：

```bash
python scripts/feedback_store.py append-profile-note \
  --scope position \
  --position-id <position_id> \
  --section hard \
  --status confirmed \
  --note "HR确认后的偏好"
```

## 质量要求

- 不手写 examples/profile/history，避免格式漂移。
- 每个岗位示例最多 50 条，脚本会清理最旧示例。
- 示例用于学习 HR 判断，不要求记录完整简历。
- HR 前后矛盾时，先问“以上次还是这次为准？”
