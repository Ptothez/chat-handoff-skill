"""Generate the single human handoff catalog from the registry, never from guesses."""
from archive_layout import ARCHIVE, BASE, REGISTRY, configured_zone, relative_from_archive, find_root
from collections import defaultdict
from pathlib import Path

KINDS = {"mainline": "主线", "special": "专项", "event": "事件", "summary": "复核材料"}


def cell(value):
    return str(value).replace("|", "／").replace("\n", " ")


def anchor(ident):
    return "entry-" + ident


def history(registry, topic):
    sources = {s["id"]: s for s in registry["sources"]}
    groups = {}
    for span in topic.get("ranges", []):
        item = groups.setdefault(span["source_id"], {"dates": [], "spans": []})
        item["dates"].append(span["occurred"])
        item["spans"].append(span)
    rows = []
    for ident, item in groups.items():
        source = sources[ident]
        lo, hi = min(item["dates"]), max(item["dates"])
        period = lo if lo == hi else lo + " 至 " + hi
        label = source.get("display_name", source.get("filename", ident))
        event = next((e for e in reversed(registry.get("events", [])) if e.get("source_id") == ident), {})
        hints = list(dict.fromkeys(s["previous_title"] for s in item["spans"] if s.get("previous_title")))
        hint = event.get("decision") or "；".join(hints) or "历史未单列增量说明，按来源原文核对"
        rows.append({"source_id": ident, "period": period, "last": hi,
                     "actor": source.get("actor", "未记录"),
                     "submitted_at": source.get("submitted_at", "历史未记录"),
                     "path": source["path"], "label": label, "hint": hint, "count": len(item["spans"]),
                     "line": item["spans"][0]["line"]})
    return rows


def navigation_bytes(registry):
    topics = registry["topics"]
    by_id = {t["id"]: t for t in topics}
    projects = {p["id"]: p for p in registry.get("projects", [])}
    grouped = defaultdict(list)
    for topic in topics:
        grouped[topic.get("project_id", topic["title"].split("-", 1)[0])].append(topic)
    lines = ["# 聊天交接总览与归档日志", "",
        f"> 归档目录：`{ARCHIVE}`。原始对话用于接手工作；本页只负责找记录、判断承接位置和追溯归档，不替代原文或业务真源。", "",
        "## 先看这里", "",
        "1. 先找具体目标，再读候选入口的梗概、承接范围和相关原文；同阶段的连续推进不按功能或 Agent 拆散。",
        "2. 已有事项复用登记前缀和稳定 ID。新目标可以使用新前缀；实质新边界用人话提出建议并确认，不能生套旧名称。",
        "3. 旧文件名先解析，不因改名另建一份。使用 `topic_archive.py . resolve <旧名或ID>`。",
        "4. 暂存原稿在 `incoming/`；审阅路由后才能提交。`originals/` 是永久证据，不是可追加入口，也不表示待办。",
        "5. 所有写入走统一提交工具。先完成阅读，再短暂加锁；遇到版本变化先重读，未完成事务先恢复，不能手改 JSON 或 README 绕过。",
        "6. 文件锁只协调本机；不同机器上的独立同步副本必须约定由一台提交，另一台等待同步与校验。不能把同步盘当跨机事务库。", "",
        "- 机器真源：[主题注册表](<" + relative_from_archive(REGISTRY) + ">)，本页由它生成。",
        "- 当前修订：`" + str(registry.get("revision", "历史未编号")) + "`；登记 " + str(len(topics)) + " 个入口。",
        "- 日期分开记：文件日期＝最后实际会话日期；日志提交时间＝写入时间。历史没有记过的提交人／提交时间不补猜。",
        "- 原文中的旧标题、阶段号及路径属于历史表述，原字保留；当前归属与追加入口看本页和稳定 ID，不据旧 H1 再建同名档。",
        "- 梗概是定位说明，不是当前完成状态；接手执行仍读对应原文、项目真源及真实待办。", "",
        "> 隐私提示：部分历史原文含凭据或个人信息，本页不复述；跨授权范围分享前另行处理，不擅自改写原档。", "",
        "## 已登记前缀（可扩展）", "",
        "、".join(projects[k]["prefix"] if k in projects else k for k in grouped) + "。", "",
        "下列名称不是白名单；没有合适目标时可以经确认登记新目标。", ""]
    lines += ["## 全量入口速查", "", "先读本表，再按候选稳定 ID 查下面的目标、范围和历史；不需要每次通读所有来源日志。", "",
              "| 稳定 ID | 类型 | 聊天主题 | 最后实际会话 |", "| --- | --- | --- | --- |"]
    project_order = {key: i for i, key in enumerate(grouped)}
    for topic in sorted(topics, key=lambda t: (project_order.get(t.get("project_id", t["title"].split("-", 1)[0]), 999),
                        0 if t.get("kind") == "mainline" else 1, t.get("stage_order", 999), t["title"])):
        lines.append(f"| `{topic['id']}` | {KINDS.get(topic.get('kind'), '事件')} | [{topic['title']}](#{anchor(topic['id'])}) | {topic['last_conversation_date']} |")
    lines.append("")
    for project_id, entries in grouped.items():
        project = projects.get(project_id, {})
        prefix = project.get("prefix", project_id)
        lines += ["## " + prefix, ""]
        if project:
            lines += ["- 主目标：" + project["goal"], "- 推进路线：" + project["current_line"],
                      "- 依据：" + project.get("goal_basis", "未记录"), ""]
            if project.get("goal_status") != "confirmed":
                lines += ["> 历史整理的目标理解，尚不等于用户逐项确认。", ""]
        ordered = sorted(entries, key=lambda t: (0 if t.get("kind") == "mainline" else 1,
                                                  t.get("stage_order", 999), t["title"]))
        lines += ["| 类型 | 原文入口 | 最后实际会话 | 承接说明 |", "| --- | --- | --- | --- |"]
        for topic in ordered:
            lines.append(f"| {KINDS.get(topic.get('kind'), '事件')} | [{topic['title']}](<{Path(topic['path']).name}>) | {topic['last_conversation_date']} | [范围与历史](#{anchor(topic['id'])}) |")
        lines.append("")
        for topic in ordered:
            handoff = topic.get("handoff", {})
            rows = history(registry, topic)
            lines += [f'<a id="{anchor(topic["id"])}"></a>', "", "### " + topic["title"], "",
                      "- 稳定 ID：`" + topic["id"] + "`。",
                      "- 梗概：" + handoff.get("synopsis", topic["scope"]),
                      "- 适合续接：" + handoff.get("include", topic["scope"]),
                      "- 不自动混入：" + handoff.get("exclude", "目标、阶段或专项不同的内容；先读原文判断，不能只按关键词或同目录追加。")]
            if not handoff:
                lines += ["- 审阅状态：沿用历史范围，尚未补做本轮语义审阅。"]
            else:
                lines += ["- 审阅依据：" + handoff.get("basis", "未提供定位依据")]
            latest_event = next((e for e in reversed(registry.get("events", [])) if topic["id"] in e.get("topic_ids", [])), None)
            if latest_event:
                lines.append("- 最近归档维护：" + latest_event["at"] + "，" + latest_event.get("actor", "未记录") + "，" + latest_event["type"] + "（维护时间，不是会话日期）。")
            elif handoff and registry.get("organized_at"):
                lines.append("- 全量审阅日期：" + registry["organized_at"] + "；历史原稿的提交人和提交时间另见来源表，不补猜。")
            related = [i for i in topic.get("related", []) if i in by_id]
            if related:
                lines.append("- 关联而非自动合并：" + "；".join(f"[{by_id[i]['title']}](#{anchor(i)})" for i in related) + "。")
            aliases = [name for name, ids in registry.get("aliases", {}).items() if topic["id"] in ids]
            if aliases:
                lines.append("- 旧名解析：" + "；".join("`" + name + "`" for name in aliases) + "。")
            for prior in registry.get("prior_views", []):
                if topic["id"] in prior.get("topic_ids", []):
                    lines.append(f"- 整理前完整入口（仅取证）：[{Path(prior['old_path']).name}](<{relative_from_archive(prior['path'])}>)，SHA-256：`{prior['sha256']}`。")
            if rows:
                known = [r["submitted_at"] for r in rows if r["submitted_at"] != "历史未记录"]
                lines += ["- 最后实际会话：" + topic["last_conversation_date"] + "；最近可证提交：" + (max(known) if known else "历史未记录") + "。", "",
                          "| 来源／原文定位 | 内容线索（非新结论） | 会话覆盖 | 归档提交时间 | 录入 Agent | 纳入区间数 |",
                          "| --- | --- | --- | --- | --- | ---: |"]
                for row in rows:
                    lines.append(f"| [{cell(row['label'])}](<{relative_from_archive(row['path'])}>)，L{row['line']} 起；ID：\u0060{row['source_id']}\u0060 | {cell(row['hint'])} | {row['period']} | {cell(row['submitted_at'])} | {cell(row['actor'])} | {row['count']} |")
            lines.append("")
    lines += ["## 结构与追加日志", "", "历史追加可由上面的来源区间追溯；没有证据的操作者与提交时间仍记未知。以下事件从启用新日志起记录。", "",
              "| 提交时间 | 操作者 | 事件 | 入口 | 说明 |", "| --- | --- | --- | --- | --- |"]
    for event in registry.get("events", []):
        links = "、".join(f"[{i}](#{anchor(i)})" for i in event.get("topic_ids", []) if i in by_id)
        lines.append(f"| {cell(event['at'])} | {cell(event.get('actor', '未记录'))} | {cell(event['type'])} | {links} | {cell(event.get('decision', ''))} |")
    return ("\n".join(lines) + "\n").encode("utf-8")
