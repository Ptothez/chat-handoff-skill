# Chat Handoff

人工触发、原文留存、面向多个 Agent 的本地工作交接 Skill。

Manually preserve original conversations locally so another agent can continue the work.

当同一项工作在不同 Agent 之间切换时，最有价值的上下文往往不只是结论，还包括用户原话、讨论过程、条件、否定和选择依据。这个 Skill 将用户选择留存的交互历史保存为可追溯的本地资产，并维护一份交接总览，帮助后续 Agent 找到正确的上下文和追加位置。

它不是自动监控器，不会后台采集全部历史，也不会把聊天上传到 GitHub 或任何服务。只有用户明确要求导出、追加或整理时才执行。公开仓库只包含通用 Skill、脚本和说明，没有聊天记录、个人资料、真实注册表或私人工作区配置。

## 核心做法

- **原文是交接材料。** 保留完整用户／助手交互；摘要只帮助定位，不代替原话。
- **按目标续接。** 同一阶段的连续推进放在一起；独立专项可单列。不按窗口、Agent 品牌或功能数量机械拆档。
- **分类可扩展。** 没有固定业务前缀、Agent 数量或阶段数量；真正的新目标或边界先用简单语言与用户确认。
- **一份总览。** 登记目标、梗概、适合追加的内容、排除范围、关系、来源及历史，生成唯一 README。
- **原文不可变。** 完整来源永久保留，日常追加验证旧字节前缀；授权重排时保留完整旧入口与区间哈希。
- **日期不冒充。** 文件前缀取最后实际会话日期，另记提交时间，不用整理当天替代。
- **共同维护。** 稳定 ID、旧名解析、精确重复检查、版本核对、本机写锁和可恢复事务共同保护交接区。

## 安装

```bash
npx -y skills add Ptothez/chat-handoff-skill -g --all
```

也可以将 `skills/chat-handoff/` 放入所用 Agent 支持的 Skill 目录。无需安装特定模型或使用特定工作区结构。运行依赖为 Python 3.10+；改名、合并及路径引用扫描需要 `rg`。只有读取压缩原生会话时才需要可选的 `zstandard` 包。

## 使用

对支持 Skill 的 Agent 说：

> 把这次交互保留到本地，先看已有交接日志，判断应该追加到哪一份。保留双方原文，不要用总结代替。

或：

> 检查已有聊天的归属。需要改名或合并时，先核对引用，保留完整旧稿和原文来源。

首次使用，Agent 应与你确定工作区和存放位置，再运行初始化。下例中的 `<skill>/scripts` 指实际安装目录，不是要原样执行的占位路径。

```bash
python <skill>/scripts/topic_archive.py <workspace> init
python <skill>/scripts/topic_archive.py <workspace> verify
```

默认目录是通用的 `chat-history/`，元数据在 `.chat-handoff/`。两者都相对于用户选定的工作区；可在初始化前用 `CHAT_HANDOFF_ARCHIVE`、`CHAT_HANDOFF_METADATA` 设置其他相对位置，以后所有提交者保持一致。不要为了安装 Skill 更名或迁移现有资料。时间默认使用系统本地时区，也可以统一设置 `CHAT_HANDOFF_TIMEZONE` 为 IANA 时区；系统缺少时区数据时需要 `tzdata`。

```text
<workspace>/
  chat-history/
    README.md                 目标、梗概、追加入口与历史
    YYYY-MM-DD_..._original.md 当前可追加的原文入口
    incoming/                 唯一暂存稿
    originals/                完整永久来源及整理前旧入口
  .chat-handoff/
    registry.json             结构化真源
    transactions/             提交证据与恢复日志
  .trash/                     可恢复退役文件与提交前备份
```

完整流程及命令见 [SKILL.md](skills/chat-handoff/SKILL.md)、[提交协议](skills/chat-handoff/references/submission.md) 和 [取源说明](skills/chat-handoff/references/native-export.md)。内置读取器覆盖部分 Claude Code、Codex、Kimi Code、Harness 和 ZCode 原生格式；不保证未来版本始终兼容，也不要求参与者属于这些平台。不支持的格式须先验证适配，不能静默略过消息。

## 隐私和安全边界

聊天可能包含密钥、身份信息、商业资料和附件。归档在本地不等于内容已经脱敏，也不等于已设置访问控制。只读取用户授权的会话；未知身份或来源无法核实时停止。对外分享原文必须另有授权，不能因为公开了本 Skill 就公开交接区。

原生记录只读，默认保存可见文字，不导出隐藏思考或工具记录。附件不自动复制；文字稿的完整性不代表附件齐全，也不代表原生日志缺口已恢复。

写锁只协调同一台机器上的合作进程。多个机器的独立副本应约定一个提交方，再同步校验；本 Skill 不提供分布式事务、远端同步或自动备份。手动绕过工具的写入也不能被绝对禁止。

删除默认移入可恢复垃圾桶并登记；不自动永久删除，不自动执行聊天中提到的任务，不自动部署或发布业务成果。历史字节完整和哈希通过不等于语义分类永远正确，后续可以有证据地纠正归属。
