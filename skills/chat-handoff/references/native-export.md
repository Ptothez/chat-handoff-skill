# 原生取源与适配

只读取用户授权的当前或指定会话。先用会话稳定 ID 和两条可见原话核对；不扫描并公开全部历史，不写原生数据库。下列目录只说明平台常见格式，不假定本机位置、版本或安装方式；优先使用当前配置和官方说明核实。

## Claude Code、Codex、Kimi Code

`export_chat.py` 可读取部分 Claude Code JSONL、Codex rollout JSONL 及 Kimi Code wire JSONL。传入实际已核实的源路径，不依赖私人工作区目录。

```bash
python <skill>/scripts/export_chat.py <native-session.jsonl> chat-history/incoming/<unique-id>.md --marker "<visible text 1>" --marker "<visible text 2>" --topic "<topic>"
```

在已初始化的工作区内运行；自定义归档目录时替换输出路径。可用 `--from-text "<唯一增量起点>"` 或带时区的 `--from-time "<ISO时间>"` 截取增量，随后仍须完整审阅。不能因为脚本成功就跳过重叠检查。

Claude Code 的可选目录定位：

```bash
python <skill>/scripts/export_chat.py --cwd <workspace> --marker "<visible text 1>" --marker "<visible text 2>" --output chat-history/incoming/<unique-id>.md
```

Codex 常见原生记录在用户数据目录的 `sessions/`，按任务 ID 定位，不选最近的 rollout。Kimi Code 先检查 `KIMI_CODE_HOME`，核对主 Agent 的 `wire.jsonl`，不能将子代理消息混作主窗口。

脚本过滤已知系统注入、隐藏思考和非可见消息，默认不含工具。新格式可能变化；发现消息数不符、角色错位、未知事件或截断时停止并核查，不容错丢消息后声称完整。传入文件名、消息 ID、消息数与至少两处原话一起复核。

## Harness／ZCode

`export_native_chat.py` 可只读解析部分 Harness 多帧 Zstandard 会话及 ZCode SQLite 会话。压缩格式需要可选 `zstandard`；SQLite 走只读事务，保留已提交 WAL 可见性。未知 schema、坏帧、重复或截断拒绝。

```bash
python <skill>/scripts/export_native_chat.py --agent dsh --source <native-session.jsonl.zstd> --session-id <id> --marker "<visible text 1>" --marker "<visible text 2>" --validate
python <skill>/scripts/export_native_chat.py --agent zcode --source <native-db.sqlite> --session-id <id> --marker "<visible text 1>" --marker "<visible text 2>" --validate
```

检查 ID、消息数、模型与附件警告后，将 `--validate` 换为 `--output chat-history/incoming/<unique-id>.md`。不要同时传 `--validate` 与 `--output`。这两个读取器只导出可见文字，不支持工具运行日志；附件仅警告，不自动复制。

## 其他 Agent 或格式

现有读取器不是参与者白名单。可以为新格式适配，但先在私有合成样本上核对角色、顺序、时间、重复边界和完整覆盖。不能把内部指令当用户发言，也不能导出模型的隐藏思考。

准备经过验证的 UTF-8 Markdown，使用标准角色和完整实际本地时间：

```text
## User YYYY-MM-DD HH:MM:SS
<用户原文>

## Assistant YYYY-MM-DD HH:MM:SS
<助手可见原文>
```

标签是格式规范，不是用户身份。模型记录实际值，缺失记未知；文件头可补来源身份、时间范围和附件缺口。正文不得通过翻译、润色或摘要替换。尚未支持的平台不能宣称已经端到端验证。

## 完整性边界

导出仅代表指定源中已核实的可见消息被留存。原生源本身缺失、附件未保存、早期导出只留摘要等情况要明示；哈希检查不能恢复不存在的文字。导出稿只是暂存，读完并路由、共同提交、全库验证后才是正式交接入口。
