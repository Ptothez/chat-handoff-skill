# 提交、日志和恢复

以下路径使用默认布局；自定义目录时所有 Agent 必须采用相同配置。`<skill>` 是安装目录。计划、检查和回执留在本地元数据目录，不放进公开 Skill。

## 路由计划

以当前注册表和目标文件的原始字节计算 SHA-256，不用文本读取后重新编码的哈希。运行 `inspect-file` 得到问答单元编号；完整阅读双方原文后准备计划。

```json
{
  "reviewed_topics": true,
  "registry_sha256": "<registry SHA-256>",
  "actor": "<actual submitting agent>",
  "decision": "<what this increment continues>",
  "source": "chat-history/incoming/<unique-id>.md",
  "sha256": "<source SHA-256>",
  "session": {
    "platform": "<open platform identifier>",
    "id": "<native session ID>",
    "previous_source_sha256": null
  },
  "routes": [{
    "topic_id": "<stable ID>",
    "unit_numbers": [1],
    "base_sha256": "<current target SHA-256>",
    "last_conversation_date": "<actual YYYY-MM-DD>",
    "handoff": {
      "synopsis": "<cumulative history synopsis>",
      "include": "<appropriate continuation>",
      "exclude": "<neighboring matters that belong elsewhere>",
      "basis": "<original message or range evidence>"
    }
  }]
}
```

`session` 首次的游标为 `null`；之后取同平台、同原生会话最近成功提交的完整来源哈希。真的缺少会话身份可省略并记缺口，不编造 ID。游标只检测并发状态，不代替逐条增量核对。

新结构另外提供：

- `structure_confirmation: {"by":"user","decision":"用户确认的建议","evidence":"当前会话原话定位"}`。`user` 是确认角色，不是私人姓名；不能伪造用户授权。
- 新目标在 `new_projects` 登记 `id/prefix/goal/current_line`。目标、前缀均可扩展，不能重复同义造名。
- 新入口在对应 route 的 `new_topic` 登记 `project_id/kind/title/scope`。`kind` 为 `mainline/special/event/summary`；原文不追加到摘要入口。主线另有正整数 `stage_order`；标题以登记 `prefix + "-"` 开头。
- 新入口不填现有 `base_sha256`。完整 `handoff` 必填。已有范围无变化可沿用现有承接说明。

每个问答一个主路由。独立段落摘取可用 `extracts: [{"unit_number":1,"start_line":3,"end_line":5}]`，行号为暂存稿物理行、包含端点；只允许完整覆盖真实内容的无损拆分。不能跨问答、重叠或漏掉回答、条件、否定和转折。完整混合来源始终永久保存。

已归档的逐字相同问答可在 `already_archived` 登记 `unit_number/source_id/range_id`；工具精确核对现存来源字节。它不再进主路由，但所有单元仍须完整计入。跨格式相似不自动跳过，回原生源核对并重新导出干净增量。确需重复留存的特殊情况在 `repeat_units` 逐条说明 `unit_number/reason`，不作为全局去重开关。

先预检，再以同一内容提交：

```bash
python <skill>/scripts/topic_archive.py . route .chat-handoff/<plan>.json
python <skill>/scripts/topic_archive.py . route .chat-handoff/<plan>.json --apply
python <skill>/scripts/topic_archive.py . verify
```

注册表变化时重新读取目录、原文和候选增量，不能只更新计划哈希。全部已归档且无新消息时报告无新增，不强造新稿。

## 路径引用

日期更新可能更名。计划的 `references` 应覆盖 `rg` 扫描的每个命中，提供 `path/sha256/action`：

- 当前功能导航用 `action: "update"`；JSON 结构化解析，字符串一次替换，不连锁替换产物。
- 原文、冻结回执、哈希／行号索引用 `action: "historical"` 并给 `reason`，继续指不可变证据。
- 固定哈希或行号依赖必须独立设计迁移；不能用新哈希伪装原验收，也不能只登记别名而留下功能悬空路径。

扫描依赖 `rg`；缺失时安装或改用经过审阅的等效流程，不跳过扫描继续改名。不要在 Skill 仓库附带用户的真实引用清单。

## 元数据纠正与历史整理

`archive_catalog.py . <plan.json>` 默认预检，`--apply` 执行。计划包含 `registry_sha256/actor/decision/structure_confirmation`，以及：

- `topics`：`id` 与需要变动的 `title/project_id/kind/scope/stage_order/related/handoff`，不改原文 ranges、日期或哈希。
- 可选 `imports`：用户授权的既有未登记完整 Markdown，带原路径、哈希、目标定义、实际日期、`date_basis` 与承接说明。来源全字节永久保存。
- 可选 `projects`：已审阅的完整目标定义；历史纠正保留依据。
- `references`：每处更名命中的迁移或历史依据。
- 可选 `source_metadata`：只补有依据的 `actor/submitted_at/basis`，不改来源身份；本次登记时间用 `imported_at`，不冒充首次导出时间。

重排已有原文区间使用 `regroup_archive.py . <plan.json>`，预检后 `--apply`。高级计划需要：`registry_sha256/actor/date/structure_confirmation/projects/topics/assignments/preview/receipt/snapshot_dir/trash_dir`。对每个旧主题提供 assignment；规则有 `default`、按区间或来源的覆盖。保留全部原区间及重复次数，完整旧入口永久复制到 `snapshot_dir`，`trash_dir` 只作恢复。

重排计划可显式登记 `legacy_entries`、完整 `imports`、`extra_evidence`、旧名映射和有原文见证的 `date_corrections`。导入分段必须物理行边界、UTF-8 有效、字节完整连续覆盖一次。此操作不是日常导出捷径；需要先读实现、生成并审阅专门计划，不自动推断语义边界。

## 锁与恢复

`writer.lock` 是本机 OS 文件锁，进程退出由 OS 释放；不删除锁 inode，不靠超时抢锁。准备事务先保存前后字节与读取依赖，逐项发布后才标 `complete`。它是可恢复提交，不是多文件瞬时原子可见。

遇到未完成事务，暂停其他提交：

```bash
python <skill>/scripts/topic_archive.py . recover
python <skill>/scripts/topic_archive.py . verify
```

恢复只在每个活跃路径仍等于登记前态或后态时继续，出现第三种内容或证据变化则保留现场、停止复核，不覆盖其他 Agent 的新写入。未完成事务所需的垃圾桶备份不得清理。

不同机器上的独立副本不共享 OS 锁，应约定一个写入方。任意外部自动同步、手工写入、恶意非合作进程不在锁的绝对保障范围内。

## 日志与验收

日志来自注册表：目标、梗概、续接范围、排除范围、关系、旧名、实际会话覆盖及逐来源录入历史。每次记录实际提交者、来源、范围、决定与提交时间，不猜历史作者或首次导出时间。

`verify` 检查来源、区间、当前入口、实际日期、别名、永久旧入口、README 同步、未登记 Markdown 和未完成事务。还要单独核对原生消息／附件缺口与语义归属；哈希不能证明这些语义判断。涉及用户的真实资料和所有运行报告留在本地。
