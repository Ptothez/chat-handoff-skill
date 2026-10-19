"""Regroup reviewed registry spans without changing evidence or inferring semantic boundaries."""
from archive_layout import ARCHIVE, BASE, REGISTRY, configured_zone, relative_from_archive, find_root

import argparse
import copy
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from archive_store import commit_locked, current_hash, json_bytes, pending, timestamp, writer_lock
from archive_navigation import navigation_bytes
from archive_references import replace_once, replace_document, rename_mapping

from topic_archive import (BASE, REGISTRY, SPEAKER, confirmed_structure, physical_lines,
                           read_json, safe, sha, validate_definition, write_navigation)


def span_key(span):
    return tuple(span[k] for k in ("source", "start", "end", "sha256", "occurred", "date_basis"))


def choose(rule, index, span):
    target = rule["default"]
    for begin, end, ident in rule.get("by_date", []):
        if begin <= span["occurred"] <= end:
            target = ident
    target = rule.get("by_source", {}).get(span["source_id"], target)
    for begin, end, ident in rule.get("ranges", []):
        if begin <= index <= end:
            target = ident
    return target


def compile_plan(root, plan):
    confirmed_structure(plan)
    registry_data = safe(root, REGISTRY).read_bytes()
    if sha(registry_data) != plan["registry_sha256"]:
        raise ValueError("Registry changed; review the latest archive before regrouping")
    old = read_json(safe(root, REGISTRY))
    legacy_entries = plan.get("legacy_entries", [])
    for entry in legacy_entries:
        if sha(safe(root, entry["path"]).read_bytes()) != entry["sha256"]:
            raise ValueError("Unregistered legacy view changed")
    definitions, rules = plan["topics"], plan["assignments"]
    if set(rules) != {t["id"] for t in old["topics"]}:
        raise ValueError("Every active archive must have exactly one reviewed assignment rule")
    projects = plan["projects"]
    if not projects:
        raise ValueError("Regroup requires open-ended registered goals")
    if len({p["id"] for p in projects}) != len(projects) or len({p["prefix"] for p in projects}) != len(projects):
        raise ValueError("Duplicate project ID or prefix")
    for p in projects:
        if not all(p.get(k) for k in ("id", "prefix", "goal", "current_line", "goal_basis", "goal_status")):
            raise ValueError("Every prefix needs a goal, current line and evidence status")
    checked = []
    for ident, definition in definitions.items():
        validate_definition(definition, projects, checked)
        if any(related not in definitions for related in definition.get("related", [])):
            raise ValueError("Related entry must resolve to an active target")
        checked.append({"id": ident, **definition})
    cached = {s["path"]: safe(root, s["path"]).read_bytes() for s in old["sources"]}
    for s in old["sources"]:
        if sha(cached[s["path"]]) != s["sha256"] or b"\0" in cached[s["path"]]:
            raise ValueError("Immutable evidence changed: " + s["path"])
    groups = {ident: [] for ident in definitions}
    imported, import_spans = [], []
    for spec in plan.get("imports", []):
        data = safe(root, spec["path"]).read_bytes()
        if sha(data) != spec["sha256"] or b"\0" in data or not spec.get("date_basis"):
            raise ValueError("Import changed or lacks reviewed actual-date evidence")
        if spec["topic_id"] not in definitions or any(s["sha256"] == sha(data) for s in old["sources"]):
            raise ValueError("Import needs a new reviewed topic and unrepresented complete source")
        ident = "legacy-" + sha(data)[:16]
        snapshot = ARCHIVE + '/originals/increments/' + ident + "_" + Path(spec["path"]).name
        if safe(root, snapshot).exists():
            raise ValueError("Import snapshot collision")
        segments = spec.get("segments", [{"start": 0, "end": len(data), "topic_id": spec["topic_id"],
                        "occurred": spec["last_conversation_date"], "date_basis": spec["date_basis"]}])
        segments = sorted(segments, key=lambda s: s["start"])
        cursor = 0
        for segment in segments:
            if (segment["start"] != cursor or not cursor < segment["end"] <= len(data)
                    or segment["topic_id"] not in definitions or not segment.get("date_basis")):
                raise ValueError("Reviewed import segments must cover every byte once without gaps")
            if ((segment["start"] and data[segment["start"] - 1:segment["start"]] != b"\n")
                    or (segment["end"] < len(data) and data[segment["end"] - 1:segment["end"]] != b"\n")):
                raise ValueError("Reviewed import boundaries must be physical line boundaries")
            data[segment["start"]:segment["end"]].decode("utf-8-sig")
            cursor = segment["end"]
        if cursor != len(data):
            raise ValueError("Reviewed import omits trailing source bytes")
        origin = {"id": ident, "filename": Path(spec["path"]).name, "path": snapshot,
                  "sha256": sha(data), "bytes": len(data), "topic_ids": sorted({s["topic_id"] for s in segments}),
                  "actor": spec.get("actor", "未记录"), "imported_from": spec["path"],
                  "review_basis": spec["date_basis"]}
        imported.append(origin)
        cached[snapshot] = data
        for number, segment in enumerate(segments, 1):
            start, end, target = segment["start"], segment["end"], segment["topic_id"]
            span = {"id": ident + f":part{number:04}", "source_id": ident, "source": snapshot,
                "start": start, "end": end, "line": data[:start].count(b"\n") + 1,
                "end_line": data[:end].count(b"\n") + int(not data[:end].endswith(b"\n")),
                "sha256": sha(data[start:end]), "occurred": segment["occurred"], "date_basis": segment["date_basis"],
                "previous_title": definitions[target]["title"], "previous_topic_id": target, "payload": data[start:end]}
            groups[target].append(span)
            import_spans.append(span)
    corrections = plan.get("date_corrections", {})
    known_spans = {s["id"] for t in old["topics"] for s in t["ranges"]}
    if set(corrections) - known_spans:
        raise ValueError("Date correction references an unknown original span")
    for correction in corrections.values():
        if not all(correction.get(k) for k in ("occurred", "date_basis", "evidence_text")):
            raise ValueError("Date corrections need actual source evidence")
    mapping, before = {}, Counter()
    for span in import_spans:
        before[span_key(span)] += 1
    for topic in old["topics"]:
        data = safe(root, topic["path"]).read_bytes()
        if sha(data) != topic["sha256"] or b"\0" in data:
            raise ValueError("Active archive changed: " + topic["path"])
        rule = rules[topic["id"]]
        for begin, end, ident in rule.get("ranges", []):
            if not 1 <= begin <= end <= len(topic["ranges"]) or ident not in definitions:
                raise ValueError("Invalid reviewed range override")
        targets = []
        for index, span in enumerate(topic["ranges"], 1):
            chunk = cached[span["source"]][span["start"]:span["end"]]
            if sha(chunk) != span["sha256"] or data[span["target_start"]:span["target_end"]] != chunk:
                raise ValueError("Original span changed: " + span["id"])
            ident = choose(rule, index, span)
            if ident not in groups:
                raise ValueError("Unregistered target: " + ident)
            effective = span
            if span["id"] in corrections:
                correction = corrections[span["id"]]
                if correction["evidence_text"] not in chunk.decode("utf-8-sig"):
                    raise ValueError("Date correction evidence absent from original bytes")
                effective = {**span, "occurred": correction["occurred"], "date_basis": correction["date_basis"]}
            groups[ident].append({**effective, "previous_topic_id": topic["id"], "previous_title": topic["title"],
                                  "payload": chunk})
            before[span_key(effective)] += 1
            if ident not in targets:
                targets.append(ident)
        mapping[topic["id"]] = targets
    topics = []
    target_paths = set()
    for ident, spans in groups.items():
        if not spans:
            raise ValueError("Empty target: " + ident)
        definition = definitions[ident]
        spans.sort(key=lambda s: (s["occurred"], s["source_id"], s["start"], s["end"]))
        last = max(s["occurred"] for s in spans)
        version = definition.get("version", "original")
        relative = ARCHIVE + '/' + last + "_" + definition["title"] + "_" + version + ".md"
        if relative in target_paths:
            raise ValueError("Duplicate target filename")
        target_paths.add(relative)
        if safe(root, relative).exists() and relative not in {t["path"] for t in old["topics"]} | {t["path"] for t in legacy_entries} | {t["path"] for t in plan.get("imports", [])}:
            raise ValueError("Target collision: " + relative)
        imported_view = next((s for s in plan.get("imports", []) if s["topic_id"] == ident and s["path"] == relative), None)
        if imported_view and len(spans) == 1 and spans[0]["start"] == 0 and spans[0]["end"] == len(cached[spans[0]["source"]]):
            entry = spans[0]
            topics.append({"id": ident, **definition, "path": relative, "last_conversation_date": last,
                "sha256": sha(entry["payload"]), "payload": entry["payload"],
                "ranges": [{k: v for k, v in entry.items() if k != "payload"} | {"target_start": 0, "target_end": len(entry["payload"])}]})
            continue
        existing = next((t for t in old["topics"] if t["id"] == ident), None)
        if (max(old.get("version", 1), plan.get("registry_version", 2)) >= 3 and existing and existing["path"] == relative
                and Counter((s["id"], span_key(s)) for s in spans) == Counter((s["id"], span_key(s)) for s in existing["ranges"])):
            payload = safe(root, relative).read_bytes()
            topics.append({**existing, **definition, "ranges": copy.deepcopy(existing["ranges"]),
                           "payload": payload, "sha256": sha(payload)})
            continue
        project = next(p for p in projects if p["id"] == definition["project_id"])
        text = (f"# {definition['title']}\n\n- 入口 ID：`{ident}`\n- 主目标：{project['goal']}\n"
                f"- 本档范围：{definition['scope']}\n- 最后实际会话日期：{last}\n"
                "- 原文保护：以下原文区间按字节保留，未改写；整理前完整入口和完整会话快照永久保留。\n"
                "- 日期依据：优先消息时间；缺少逐条日期的旧段落沿用已核验的来源覆盖日期，不虚构精确时间。\n"
                "- 归档说明：主线保留同阶段共同目标的进程；专项独立续接。历史标题、编号和路径属于当时事实，不当成当前入口。\n\n")
        if definition.get("related"):
            text += "## 关联入口\n\n" + "\n".join(f"- `{i}`：见 README 或旧名解析。" for i in definition["related"]) + "\n\n"
        text += "## 原文进程导航\n\n"
        by_day = defaultdict(list)
        for number, span in enumerate(spans, 1):
            by_day[span["occurred"]].append((number, span))
        for day, entries in by_day.items():
            names = list(dict.fromkeys(s["previous_title"].split("-", 1)[-1] for _, s in entries))
            text += f"- [{day}](#original-{entries[0][0]})：{'；'.join(names)}（{len(entries)} 个原文区间）\n"
        payload = bytearray((text + "\n---\n").encode("utf-8"))
        ranges = []
        for number, span in enumerate(spans, 1):
            payload.extend((f"\n\n<a id=\"original-{number}\"></a>\n\n"
                            f"<!-- BEGIN_TOPIC_ORIGINAL: {span['id']} | {span['occurred']} | {span['source']} | L{span['line']}-{span['end_line']} -->\n").encode())
            start = len(payload)
            payload.extend(span["payload"])
            end = len(payload)
            payload.extend(f"\n<!-- END_TOPIC_ORIGINAL: {span['id']} -->\n".encode())
            ranges.append({k: v for k, v in span.items() if k not in ("payload", "target_start", "target_end")} |
                          {"target_start": start, "target_end": end})
        topics.append({"id": ident, **definition, "path": relative, "last_conversation_date": last,
                       "sha256": sha(payload), "ranges": ranges, "payload": bytes(payload)})
    after = Counter(span_key(s) for t in topics for s in t["ranges"])
    if before != after:
        raise ValueError("Existing original span coverage changed")
    aliases = {}
    for name, ids in old["aliases"].items():
        aliases[name] = sorted({target for ident in ids for target in mapping.get(ident, [ident])})
    for t in old["topics"]:
        aliases[t["id"]] = mapping[t["id"]]
        aliases[Path(t["path"]).name] = mapping[t["id"]]
    for entry in legacy_entries:
        if entry["target_id"] not in definitions:
            raise ValueError("Legacy view must point to a reviewed formal entry")
        aliases[Path(entry["path"]).name] = [entry["target_id"]]
    for spec in plan.get("imports", []):
        destination_ids = sorted({s["topic_id"] for s in spec.get("segments", [{"topic_id": spec["topic_id"]}])})
        aliases[Path(spec["path"]).name] = destination_ids
    for name, ids in plan.get("alias_updates", {}).items():
        if not ids or any(ident not in definitions for ident in ids):
            raise ValueError("Reviewed alias correction has no active target")
        aliases[name] = ids
    if any(not ids or any(ident not in definitions for ident in ids) for ids in aliases.values()):
        raise ValueError("Every historical alias must resolve to an active entry")
    sources = copy.deepcopy(old["sources"]) + imported
    for source in sources:
        source["topic_ids"] = sorted({t["id"] for t in topics for s in t["ranges"] if s["source_id"] == source["id"]})
    required_version = 3 if plan.get("imports") or legacy_entries else 2
    new = {**old, "version": max(required_version, old.get("version", 1), plan.get("registry_version", 2)), "organized_at": plan["date"], "projects": projects,
           "topics": [{k: v for k, v in t.items() if k != "payload"} for t in topics], "aliases": aliases,
           "sources": sources, "regroup_confirmation": plan["structure_confirmation"]}
    return old, new, topics, mapping


def find_references(root, registry):
    patterns = []
    for topic in registry["topics"]:
        name = Path(topic["path"]).name
        patterns.extend([name, json.dumps(name, ensure_ascii=True)[1:-1]])
    if not patterns:
        return []
    command = ["rg", "-l", "--hidden", "-F", "-f", "-", ".",
               "-g", "!" + ARCHIVE + "/**", "-g", "!" + BASE + "/**",
               "-g", "!.trash/**", "-g", "!**/node_modules/**", "-g", "!**/.git/**"]
    result = subprocess.run(command, input="\n".join(dict.fromkeys(patterns)) + "\n",
                            cwd=root, capture_output=True, encoding="utf-8")
    if result.returncode not in (0, 1):
        raise ValueError(result.stderr)
    return sorted(s.removeprefix(".\\").replace("\\", "/") for s in result.stdout.splitlines())



def reference_changes(root, plan, old, new, mapping):
    targets = {t["id"]: t["path"] for t in new["topics"]}
    changes = []
    for spec in plan.get("references", []):
        path = safe(root, spec["path"])
        before = path.read_bytes()
        if sha(before) != spec["sha256"]:
            raise ValueError("Reference changed: " + spec["path"])
        text = before.decode("utf-8-sig")
        if spec.get("mode") == "json_current_topics":
            value = json.loads(text)
            def visit(item):
                if isinstance(item, dict):
                    for key, v in item.items():
                        if key == "current_topic_paths":
                            replaced = []
                            for entry in v:
                                match = next((t for t in old["topics"] if t["path"] == entry), None)
                                replaced.extend(targets[i] for i in mapping[match["id"]]) if match else replaced.append(entry)
                            item[key] = list(dict.fromkeys(replaced))
                        else:
                            visit(v)
                elif isinstance(item, list):
                    for v in item:
                        visit(v)
            visit(value)
            text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        elif spec.get("mode", "navigation") == "navigation":
            renamed = []
            for topic in old["topics"]:
                ids = mapping[topic["id"]]
                target = targets[ids[0]] if len(ids) == 1 else plan["snapshot_dir"] + "/" + Path(topic["path"]).name
                renamed.append({"old": topic["path"], "new": target})
            renamed += [{"old": entry["path"], "new": targets[entry["target_id"]]}
                        for entry in plan.get("legacy_entries", [])]
            for incoming in plan.get("imports", []):
                destination_ids = sorted({s["topic_id"] for s in incoming.get("segments", [{"topic_id": incoming["topic_id"]}])})
                target = targets[destination_ids[0]] if len(destination_ids) == 1 else next(s["path"] for s in new["sources"] if s.get("imported_from") == incoming["path"])
                renamed.append({"old": incoming["path"], "new": target})
            text = replace_document(text, rename_mapping(renamed), path.suffix == ".json")
        elif spec.get("mode") == "historical":
            if not spec.get("reason", "").strip():
                raise ValueError("Historical reference requires an evidence reason")
        elif spec.get("mode") == "retain_current":
            if not spec.get("reason", "").strip() or not spec.get("topic_ids"):
                raise ValueError("Retained current reference needs explicit target IDs and reason")
            if any(ident not in targets or targets[ident] not in text for ident in spec["topic_ids"]):
                raise ValueError("Retained current reference no longer points to its reviewed target")
        elif spec.get("mode") == "reviewed_text":
            replacements = spec.get("replacements", {})
            if not replacements or any(old not in text for old in replacements):
                raise ValueError("Reviewed reference replacement does not match")
            text = replace_once(text, replacements)
        else:
            raise ValueError("Unreviewed reference mode")
        after = before if spec.get("mode") in ("historical", "retain_current") else ((b"\xef\xbb\xbf" if before.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8"))
        changes.append({"path": spec["path"], "before_sha256": sha(before), "after_sha256": sha(after),
                        "before": before, "after": after})
    return changes


def run(root, plan, apply=False):
    if apply:
        with writer_lock(root):
            if pending(root):
                raise ValueError("Pending archive transaction; recover first")
            return run_locked(root, plan, True)
    return run_locked(root, plan, False)


def run_locked(root, plan, apply=False):
    old, new, topics, mapping = compile_plan(root, plan)
    if new.get("version", 1) >= 3 and not str(plan.get("actor", "")).strip():
        raise ValueError("Transactional regroup needs the actual submitting actor")
    changes = reference_changes(root, plan, old, new, mapping)
    reference_inventory = {**old, "topics": old["topics"] + plan.get("legacy_entries", []) + plan.get("imports", [])}
    hits = find_references(root, reference_inventory)
    if set(hits) != {c["path"] for c in changes}:
        raise ValueError("Review every reference hit before changing archive paths")
    result = {"applied": apply, "old_entries": len(old["topics"]), "entries": len(topics),
              "projects": len(new["projects"]), "sources": len(new["sources"]),
              "preserved_ranges": sum(len(t["ranges"]) for t in topics), "mapping": mapping,
              "date_corrections": plan.get("date_corrections", {}),
              "imports": [{k: v for k, v in s.items() if k != "actor"} for s in plan.get("imports", [])],
              "registry_sha256": sha(json.dumps(new, ensure_ascii=False, indent=2).encode()),
              "references": [{k: v for k, v in c.items() if k not in ("before", "after")} for c in changes]}
    if not apply:
        result["reference_hits"] = hits
        safe(root, plan["preview"]).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "mapping"}, ensure_ascii=False, indent=2))
        return
    preview = read_json(safe(root, plan["preview"]))
    if preview.get("applied") or any(preview[k] != result[k] for k in result if k != "applied"):
        raise ValueError("Regroup plan differs from reviewed preflight")
    if set(find_references(root, reference_inventory)) != set(preview["reference_hits"]):
        raise ValueError("Reference inventory changed after preflight")
    trash = safe(root, plan["trash_dir"])
    if trash.exists() or safe(root, plan["snapshot_dir"]).exists():
        raise ValueError("Regroup already started or output collision")
    for change in changes:
        if safe(root, change["path"]).read_bytes() != change["before"]:
            raise ValueError("Concurrent reference edit")
    if sha(safe(root, REGISTRY).read_bytes()) != plan["registry_sha256"]:
        raise ValueError("Concurrent registry edit")
    if new.get("version", 1) >= 3:
        return transactional_regroup(root, plan, old, new, topics, changes, result)
    # Preserve complete previous derived entries, including their original headers and offsets.
    for topic in old["topics"]:
        source = safe(root, topic["path"])
        target = safe(root, plan["snapshot_dir"] + "/" + source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if sha(target.read_bytes()) != topic["sha256"]:
            raise ValueError("Previous-entry snapshot failed")
    trash.mkdir(parents=True)
    shutil.copy2(safe(root, REGISTRY), trash / "主题注册表_规整前.json")
    shutil.copy2(safe(root, ARCHIVE + '/README.md'), trash / "README_规整前.md")
    for change in changes:
        backup = trash / "引用备份" / change["path"]
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(change["before"])
    # Check all active bytes again before moving; no old entry is edited in place.
    for topic in old["topics"]:
        if sha(safe(root, topic["path"]).read_bytes()) != topic["sha256"]:
            raise ValueError("Concurrent archive edit")
    for topic in old["topics"]:
        shutil.move(str(safe(root, topic["path"])), str(trash / Path(topic["path"]).name))
    for topic in topics:
        with safe(root, topic["path"]).open("xb") as handle:
            handle.write(topic["payload"])
    for change in changes:
        path = safe(root, change["path"])
        if path.read_bytes() != change["before"]:
            raise ValueError("Concurrent reference edit")
        path.write_bytes(change["after"])
    if sha(safe(root, REGISTRY).read_bytes()) != plan["registry_sha256"]:
        raise ValueError("Concurrent registry edit")
    safe(root, REGISTRY).write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_navigation(root, new)
    with safe(root, ".trash/README.md").open("ab") as handle:
        handle.write((f"\n- `{plan['trash_dir']}/`：目标／阶段规整前的 {len(old['topics'])} 个入口及索引备份；"
                      "完整入口另有永久快照，已移至垃圾桶，30 天后可清理。\n").encode())
    safe(root, plan["receipt"]).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("mapping", "references")}, ensure_ascii=False))


def transactional_regroup(root, plan, old, new, topics, references, result):
    changes, read_guards, retired = [], [], []
    new["prior_views"] = copy.deepcopy(old.get("prior_views", []))
    captured_at = timestamp()
    for evidence in plan.get("extra_evidence", []):
        data = safe(root, evidence["path"]).read_bytes()
        if sha(data) != evidence["sha256"]:
            raise ValueError("Additional historical evidence changed")
        preserved_path = plan["snapshot_dir"] + "/" + Path(evidence["old_path"]).name
        changes.append({"path": preserved_path, "before": None, "after": data})
        read_guards.append({"path": evidence["path"], "sha256": evidence["sha256"]})
        new["prior_views"].append({"old_path": evidence["old_path"], "path": preserved_path,
            "sha256": evidence["sha256"], "bytes": len(data), "captured_at": captured_at,
            "topic_ids": evidence["topic_ids"], "purpose": "并发追加前的用户指定完整入口，永久保留历史字节和哈希，不能追加"})
    previous = {t["path"]: t for t in old["topics"]}
    previous.update({t["path"]: t for t in plan.get("legacy_entries", [])})
    previous.update({t["path"]: t for t in plan.get("imports", [])})
    targets = {t["path"] for t in topics}
    for topic in old["topics"] + plan.get("legacy_entries", []):
        data = safe(root, topic["path"]).read_bytes()
        if sha(data) != topic["sha256"]:
            raise ValueError("Concurrent archive edit")
        matching = next((t for t in topics if t["path"] == topic["path"]), None)
        if not matching or matching["sha256"] != topic["sha256"]:
            preserved_path = plan["snapshot_dir"] + "/" + Path(topic["path"]).name
            changes.append({"path": preserved_path,
                            "before": None, "after": data})
            destination_ids = result["mapping"].get(topic.get("id"), [topic.get("target_id")])
            new["prior_views"].append({"old_path": topic["path"], "path": preserved_path,
                "sha256": topic["sha256"], "bytes": len(data), "captured_at": captured_at,
                "topic_ids": [i for i in destination_ids if i], "purpose": "整理前完整入口，只作历史字节/行号/哈希取证，不能追加"})
        if topic["path"] not in targets:
            retired.append({"path": topic["path"], "before": topic["sha256"], "after": None})
    previous_source_ids = {s["id"] for s in old["sources"]}
    for source in new["sources"]:
        if source["id"] not in previous_source_ids and source.get("imported_from"):
            data = safe(root, source["imported_from"]).read_bytes()
            if sha(data) != source["sha256"]:
                raise ValueError("Concurrent import edit")
            source.setdefault("imported_at", timestamp())
            changes.append({"path": source["path"], "before": None, "after": data})
    for topic in topics:
        if previous.get(topic["path"], {}).get("sha256") == topic["sha256"]:
            read_guards.append({"path": topic["path"], "sha256": topic["sha256"]})
            continue
        changes.append({"path": topic["path"], "before": previous.get(topic["path"], {}).get("sha256"),
                        "after": topic["payload"]})
    for ref in references:
        if ref["before_sha256"] == ref["after_sha256"]:
            read_guards.append({"path": ref["path"], "sha256": ref["before_sha256"]})
        else:
            changes.append({"path": ref["path"], "before": ref["before_sha256"], "after": ref["after"]})
    for spec in plan.get("imports", []):
        if spec["path"] not in targets:
            retired.append({"path": spec["path"], "before": spec["sha256"], "after": None})
    changes += retired
    new["revision"] = old.get("revision", 0) + 1
    old_by_id = {t["id"]: t for t in old["topics"]}
    changed_ids = [t["id"] for t in topics if t["id"] not in old_by_id or any(
        t.get(k) != old_by_id[t["id"]].get(k) for k in ("sha256", "path", "project_id", "kind", "title"))]
    new.setdefault("events", []).append({"id": "regroup-" + str(new["revision"]), "type": "regroup",
        "at": timestamp(), "actor": plan["actor"], "topic_ids": changed_ids,
        "decision": plan["structure_confirmation"]["decision"], "reviewed_topic_count": len(topics),
        "changed_view_count": len(changed_ids), "confirmation": plan["structure_confirmation"]})
    for event in plan.get("correction_events", []):
        if any(i not in {t["id"] for t in topics} for i in event["topic_ids"]):
            raise ValueError("Correction event points to an unknown active topic")
        new["events"].append({**event, "at": timestamp(), "actor": plan["actor"]})
    registry_data = json_bytes(new)
    result["registry_sha256"] = sha(registry_data)
    nav = ARCHIVE + '/README.md'
    changes.extend([{"path": REGISTRY, "before": plan["registry_sha256"], "after": registry_data},
                    {"path": nav, "before": current_hash(safe(root, nav)), "after": navigation_bytes(new)},
                    {"path": plan["receipt"], "before": None, "after": json_bytes(result)}])
    guards = read_guards + [{"path": s["path"], "sha256": s["sha256"]} for s in old["sources"]]
    result["transaction"] = commit_locked(root, changes, "原文区间重新编排", guards=guards)["id"]
    print(json.dumps({k: v for k, v in result.items() if k not in ("mapping", "references")}, ensure_ascii=False))
    return result


def review(root, ids, start, limit, chars, full=False, human=False, unique=False):
    registry = read_json(safe(root, REGISTRY))
    for topic in registry["topics"]:
        if ids != "all" and topic["id"] not in ids.split(","):
            continue
        print("\n###", topic["id"], topic["title"], topic["scope"])
        seen = set()
        for number, span in enumerate(topic["ranges"], 1):
            if not start <= number < start + limit:
                continue
            text = safe(root, span["source"]).read_bytes()[span["start"]:span["end"]].decode("utf-8-sig")
            if full:
                print(number, span["occurred"], span["source_id"], text)
                continue
            lines = text.split("\n")
            boundary = next((i for i, line in enumerate(lines) if SPEAKER.match(line.rstrip("\r"))
                             and SPEAKER.match(line.rstrip("\r"))["role"] not in ("User", "User", "用户")), len(lines))
            question = "\n".join(lines[:boundary])
            question = re.sub(r"<channel\b[^>]*>|</channel>", "", question)
            if re.search(r"\[(?:tool result|工具返回(?:结果)?)\]|<local-command-|Base directory for this skill:|<environment_context>|<turn_aborted>|The TodoWrite tool hasn't|<notification |<task-notification>|<command-message>|# AGENTS.md instructions|<recommended_plugins>|Skill tool loaded instructions|<subagent_notification>|Stop hook feedback:|This session is being continued", question):
                if not human:
                    print(number, span["occurred"], span["source_id"], "[历史内部指令／续接上下文，原字节保留]")
                continue
            if "## My request" in question:
                question = re.split(r"## My request[^\n]*\n", question)[-1]
            answer = "\n".join(lines[boundary:])
            headings = [line for line in answer.split("\n") if line.startswith(("## ", "### ", "#### ")) and not SPEAKER.match(line.rstrip("\r"))]
            question = re.sub(r"^#{2,3}[^\n]*\n", "", question)
            key = re.sub(r"\s+", "", question)
            if unique and key in seen:
                continue
            seen.add(key)
            print(number, span["occurred"], span["source_id"], re.sub(r"\s+", " ", question)[:chars],
                  "| 答：", " / ".join(headings)[:chars] or re.sub(r"\s+", " ", answer)[-min(chars, 500):])


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("review")
    p.add_argument("ids")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--limit", type=int, default=10000)
    p.add_argument("--chars", type=int, default=500)
    p.add_argument("--full", action="store_true")
    p.add_argument("--human", action="store_true")
    p.add_argument("--unique", action="store_true")
    p = sub.add_parser("preflight")
    p.add_argument("plan")
    p = sub.add_parser("apply")
    p.add_argument("plan")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "review":
        review(root, args.ids, args.start, args.limit, args.chars, args.full, args.human, args.unique)
    else:
        run(root, read_json(safe(root, args.plan)), args.command == "apply")


if __name__ == "__main__":
    main()
