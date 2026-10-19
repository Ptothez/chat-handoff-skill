"""Inspect and route reviewed byte ranges; never infer topics from keywords."""
from archive_layout import ARCHIVE, BASE, REGISTRY, configured_zone, relative_from_archive, find_root

import argparse
import hashlib
import json
import re
import sys
import shutil
from datetime import date, timedelta
from pathlib import Path

from archive_store import (commit_locked, current_hash, json_bytes, pending,
                           recover, timestamp, writer_lock)


DATE = re.compile(r"20\d{2}-\d{2}-\d{2}")
SPEAKER = re.compile(
    r"^#{2,3} (?:20\d{2}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})? )?"
    r"(?:\d+\. )?(?:\*\*)?(?:[🧑🤖] ?)?"
    r"(?P<role>User|用户|User|助手|Assistant|Claude|Codex|DeepSeek|GLM|Kimi)"
    r"(?=(?:\*\*)?(?:$|[（(：:]|\s+[（(]|\s+(?:20\d{2}-|\d{1,2}[:/])))"
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Outside workspace: {relative}")
    return path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def physical_lines(data):
    parts = data.split(b"\n")
    return [part + b"\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def question_key(question):
    text = re.sub(r"^#{2,3}[^\n]*\n", "", question)
    text = text.split("\n---", 1)[0]
    text = re.sub(r"<channel\b[^>]*>|</channel>", "", text)
    return re.sub(r"\s+", "", text)


def unit_date(unit, filename):
    """Use message evidence, never export time, mtime, or dates in user prose."""
    heading_dates = DATE.findall(unit["heading"])
    if heading_dates:
        return heading_dates[-1], "message_heading"
    context = unit["date_context"]
    dates = DATE.findall(context)
    ceiling = filename[:10]
    if dates:
        valid = [d for d in dates if d <= ceiling]
        if valid:
            return valid[-1], "source_conversation_range_end"
    return ceiling, "source_last_conversation_date"




def resolve(root, query):
    registry = read_json(safe(root, REGISTRY))
    name = query.replace("\\", "/").rsplit("/", 1)[-1]
    matches = [t for t in registry["topics"] if query == t["id"] or name == Path(t["path"]).name]
    alias_key = query if query in registry["aliases"] else name
    if not any(t["id"] == query for t in registry["topics"]) and len(registry["aliases"].get(alias_key, [])) > 1:
        matches = [t for t in registry["topics"] if t["id"] in registry["aliases"][alias_key]]
    if not matches:
        ids = registry["aliases"].get(name, [])
        matches = [t for t in registry["topics"] if t["id"] in ids]
    from archive_navigation import history
    projects = {p["id"]: p for p in registry.get("projects", [])}
    evidence = [v for v in registry.get("prior_views", []) if query in (v["sha256"], v["old_path"], Path(v["old_path"]).name)
                or any(t["id"] in v.get("topic_ids", []) for t in matches)]
    print(json.dumps({"query": query, "registry_sha256": sha(safe(root, REGISTRY).read_bytes()),
        "historical_evidence_only": evidence, "matches": [{k: t[k] for k in
        ("id", "title", "scope", "path", "last_conversation_date")} |
        {k: t[k] for k in ("project_id", "kind", "stage_order", "related", "handoff") if k in t} |
        {"project": projects.get(t.get("project_id"), {}), "source_history": history(registry, t)} for t in matches],
        "requires_topic_review": len(matches) != 1}, ensure_ascii=False, indent=2))




def verify(root):
    registry = read_json(safe(root, REGISTRY))
    failures = []
    originals = {}
    for source in registry["sources"]:
        originals[source["path"]] = safe(root, source["path"]).read_bytes()
        if sha(originals[source["path"]]) != source["sha256"] or b"\0" in originals[source["path"]]:
            failures.append(source["path"])
    checked = []
    for topic in registry["topics"]:
        if registry.get("version", 1) >= 2:
            try:
                if not registry.get("projects"):
                    raise ValueError("Missing goals")
                validate_definition(topic, registry["projects"], checked)
            except ValueError as error:
                failures.append(topic["id"] + ": " + str(error))
        checked.append(topic)
        data = safe(root, topic["path"]).read_bytes()
        if sha(data) != topic["sha256"] or b"\0" in data:
            failures.append(topic["path"])
        for span in topic["ranges"]:
            original = originals[span["source"]][span["start"]:span["end"]]
            if sha(original) != span["sha256"] or data[span["target_start"]:span["target_end"]] != original:
                failures.append(span["id"])
        if topic["ranges"] and (topic["last_conversation_date"] != max(s["occurred"] for s in topic["ranges"])
                                or not Path(topic["path"]).name.startswith(topic["last_conversation_date"] + "_")):
            failures.append(topic["id"] + ": date mismatch")
    ids = {t["id"] for t in registry["topics"]}
    for name, targets in registry["aliases"].items():
        if not targets or any(ident not in ids for ident in targets):
            failures.append("alias: " + name)
    for prior in registry.get("prior_views", []):
        path = safe(root, prior["path"])
        if not path.is_file() or sha(path.read_bytes()) != prior["sha256"]:
            failures.append("historical entry evidence: " + prior["path"])
    if registry.get("version", 1) >= 3:
        from archive_navigation import navigation_bytes
        if safe(root, ARCHIVE + '/README.md').read_bytes() != navigation_bytes(registry):
            failures.append("README differs from the shared registry")
        registered = {t["path"] for t in registry["topics"]}
        for path in safe(root, ARCHIVE).glob("*.md"):
            if path.name != "README.md" and path.relative_to(root).as_posix() not in registered:
                failures.append("unregistered active entry: " + path.name)
        if pending(root):
            failures.append("unfinished transaction")
    print(json.dumps({"pass": not failures, "sources": len(registry["sources"]),
        "topics": len(registry["topics"]), "failures": failures}, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


def write_navigation(root, registry):
    from archive_navigation import navigation_bytes
    safe(root, ARCHIVE + '/README.md').write_bytes(navigation_bytes(registry))
    return




def confirmed_structure(manifest):
    confirmation = manifest.get("structure_confirmation", {})
    if (str(confirmation.get("by", "")).casefold() not in ("user", "用户")
            or not confirmation.get("decision", "").strip()
            or not confirmation.get("evidence", "").strip()):
        raise ValueError("New archive boundary requires user structure_confirmation, decision and evidence")


def route_projects(registry, manifest):
    projects = list(registry.get("projects", []))
    if manifest.get("new_projects") or manifest.get("project_updates"):
        confirmed_structure(manifest)
    for project in manifest.get("new_projects", []):
        if any(p["id"] == project["id"] or p["prefix"] == project["prefix"] for p in projects):
            raise ValueError("Project ID or prefix already registered; reuse its goal")
        if not all(isinstance(project.get(k), str) and project[k].strip()
                   for k in ("id", "prefix", "goal", "current_line")):
            raise ValueError("New project needs ID, prefix, goal and current_line")
        if any(c in project["prefix"] for c in '-/\\:<>"|?*'):
            raise ValueError("Project prefix must be a safe filename component")
        projects.append({**project, "goal_basis": project.get("goal_basis") or manifest["structure_confirmation"]["evidence"],
                         "goal_status": "confirmed", "confirmation": manifest["structure_confirmation"]})
    for update in manifest.get("project_updates", []):
        index = next((i for i, p in enumerate(projects) if p["id"] == update["id"]), None)
        if index is None or "prefix" in update:
            raise ValueError("Unknown project or prefix rename requires a reviewed regroup migration")
        projects[index] = {**projects[index], **update, "goal_status": "confirmed",
                           "confirmation": manifest["structure_confirmation"]}
    return projects


def validate_definition(definition, projects, topics):
    title = definition.get("title", "")
    if not isinstance(title, str) or not title.strip() or any(c in title for c in '/\\:<>"|?*\r\n\0'):
        raise ValueError("Archive title must be a safe filename component")
    if not projects:
        return
    project = next((p for p in projects if p["id"] == definition.get("project_id")), None)
    if not project or not definition["title"].startswith(project["prefix"] + "-"):
        raise ValueError("Archive must reference its registered goal and matching prefix")
    if project.get("status") == "retired":
        raise ValueError("Project classification retired; read its correction and current parent")
    if definition.get("kind") not in ("mainline", "special", "event", "summary"):
        raise ValueError("Archive needs mainline, special, event or summary kind")
    if definition["kind"] == "mainline":
        stage = definition.get("stage_order")
        if not isinstance(stage, int) or isinstance(stage, bool) or stage < 1:
            raise ValueError("Mainline needs a positive stage_order")
        if any(t.get("project_id") == project["id"] and t.get("kind") == "mainline"
               and t.get("stage_order") == stage for t in topics):
            raise ValueError("This mainline stage already exists; append rather than fragment it")


def reviewed_units(record, data, route):
    lines = physical_lines(data)
    offsets = [0]
    line_dates = []
    current_date = None
    for line in lines:
        offsets.append(offsets[-1] + len(line))
        heading = line.decode("utf-8-sig").rstrip("\r\n")
        if SPEAKER.match(heading) and DATE.search(heading):
            current_date = DATE.findall(heading)[-1]
        line_dates.append(current_date)
    cuts = []
    for extract in route.get("extracts", []):
        number, begin, end = extract["unit_number"], extract["start_line"], extract["end_line"]
        if not 1 <= number <= len(record["units"]) or not 1 <= begin <= end <= len(lines):
            raise ValueError("Invalid reviewed paragraph coordinates")
        parent = record["units"][number - 1]
        start, stop = offsets[begin - 1], offsets[end]
        if not parent["start"] <= start < stop <= parent["end"]:
            raise ValueError("Reviewed paragraph crosses dialogue boundaries")
        dates = [d for d in line_dates[begin - 1:end] if d]
        if not dates:
            raise ValueError("Reviewed paragraph lacks dated message context")
        cuts.append({**parent, "id": f"INCOMING:L{begin}-{end}", "start": start, "end": stop,
                     "line": begin, "end_line": end, "sha256": sha(data[start:stop]),
                     "parent_unit_id": parent["id"], "occurred": max(dates)})
    ordered = sorted(cuts, key=lambda c: c["start"])
    if any(a["end"] > b["start"] for a, b in zip(ordered, ordered[1:])):
        raise ValueError("Overlapping paragraph extracts within one topic")
    primary = [record["units"][n - 1] for n in sorted(route["unit_numbers"])]
    chosen = [u for u in primary if not any(c["parent_unit_id"] == u["id"] for c in cuts)] + cuts
    if not chosen:
        raise ValueError("Topic route has no reviewed original content")
    return sorted(chosen, key=lambda u: u["start"])


def validate_content_coverage(record, data, manifest):
    intervals = [(u["start"], u["end"]) for route in manifest["routes"]
                 for u in reviewed_units(record, data, route)]
    intervals += [(record["units"][x["unit_number"] - 1]["start"],
                   record["units"][x["unit_number"] - 1]["end"]) for x in manifest.get("already_archived", [])]
    intervals.sort()
    merged = []
    for begin, end in intervals:
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((begin, end))
    offset = 0
    first = record["units"][0]["start"]
    for number, raw in enumerate(physical_lines(data), 1):
        end = offset + len(raw)
        line = raw.decode("utf-8-sig").strip()
        if offset >= first and line and line != "---" and not SPEAKER.match(line):
            if not any(begin <= offset and end <= stop for begin, stop in merged):
                raise ValueError(f"Paragraph routing omits message content at L{number}; preserve the answer and conditions")
        offset = end


def append_topics(root, manifest, apply=False):
    if apply:
        with writer_lock(root):
            return _append_topics(root, manifest, True)
    return _append_topics(root, manifest, False)


def _append_topics(root, manifest, apply=False):
    if pending(root):
        raise ValueError("Pending archive transaction; run recover first")
    if manifest.get("reviewed_topics") is not True:
        raise ValueError("Read and review every topic boundary before routing")
    source = safe(root, manifest["source"])
    if not source.relative_to(root).as_posix().startswith(ARCHIVE + '/incoming/'):
        raise ValueError("Incoming source must be a staging export")
    data = source.read_bytes()
    if sha(data) != manifest["sha256"] or b"\0" in data:
        raise ValueError("Incoming export changed")
    registry_bytes = safe(root, REGISTRY).read_bytes()
    registry = read_json(safe(root, REGISTRY))
    if registry.get("version", 1) >= 3:
        if manifest.get("registry_sha256") != sha(registry_bytes):
            raise ValueError("Registry revision changed; reread the shared handoff log")
        if not isinstance(manifest.get("actor"), str) or not manifest["actor"].strip():
            raise ValueError("Record the submitting actor; no fixed Agent whitelist")
        if not manifest.get("decision", "").strip():
            raise ValueError("Record what this increment continues")
        for route in manifest["routes"]:
            existing = next((t for t in registry["topics"] if t["id"] == route["topic_id"]), {})
            handoff = route.get("handoff", existing.get("handoff", {}))
            if not all(isinstance(handoff.get(k), str) and handoff[k].strip()
                       for k in ("synopsis", "include", "exclude", "basis")):
                raise ValueError("Read the topic and provide its handoff synopsis, boundaries and evidence")
    projects = route_projects(registry, manifest)
    if registry.get("version", 1) >= 2 and not projects:
        raise ValueError("Goal registry missing; review before routing")
    record = scan(source, "INCOMING")
    if not record["units"]:
        raise ValueError("No user dialogue units found; inspect the export format")
    unit_dates = {}
    for unit in record["units"]:
        text = data[unit["start"]:unit["end"]].decode("utf-8")
        dates = [DATE.findall(line)[-1] for line in text.splitlines()
                 if SPEAKER.match(line) and DATE.search(line)]
        if not dates:
            raise ValueError("Missing actual message date; re-export with full local timestamps")
        unit_dates[unit["id"]] = max(dates)
    skipped = manifest.get("already_archived", [])
    source_lookup = {s["id"]: s for s in registry["sources"]}
    for item in skipped:
        if not 1 <= item["unit_number"] <= len(record["units"]):
            raise ValueError("Invalid already_archived unit number")
        unit = record["units"][item["unit_number"] - 1]
        prior = source_lookup.get(item["source_id"])
        span = next((s for t in registry["topics"] for s in t["ranges"]
                     if s["source_id"] == item["source_id"] and s["id"] == item["range_id"]), None)
        if not prior or not span:
            raise ValueError("Skipped unit needs an existing archived original range")
        old_data = safe(root, prior["path"]).read_bytes()
        if sha(old_data) != prior["sha256"] or data[unit["start"]:unit["end"]] != old_data[span["start"]:span["end"]]:
            raise ValueError("Overlap is not byte-identical; inspect and export a clean increment")
    selected = ([n for route in manifest["routes"] for n in route["unit_numbers"]]
                + [x["unit_number"] for x in skipped])
    if sorted(selected) != list(range(1, len(record["units"]) + 1)):
        raise ValueError("Each incoming dialogue unit must have exactly one primary topic")
    validate_content_coverage(record, data, manifest)
    if len({r["topic_id"] for r in manifest["routes"]}) != len(manifest["routes"]):
        raise ValueError("Combine all ranges for a topic into one reviewed route")
    source_id = "incoming-" + sha(data)[:16]
    if any(s["sha256"] == sha(data) for s in registry["sources"]):
        raise ValueError("This complete export has already been archived")
    if registry.get("version", 1) >= 3:
        existing_hashes = {s["sha256"] for t in registry["topics"] for s in t["ranges"]}
        excluded = {x["unit_number"] for x in skipped}
        repeats = manifest.get("repeat_units", [])
        if any(not 1 <= x.get("unit_number", 0) <= len(record["units"]) or not x.get("reason", "").strip() for x in repeats):
            raise ValueError("Repeat exception needs a unit number and reason")
        allowed = {x["unit_number"] for x in repeats}
        for number, unit in enumerate(record["units"], 1):
            if number not in excluded | allowed and unit["sha256"] in existing_hashes:
                raise ValueError("Incoming unit already archived; inspect incremental boundary or document repeat")
    session = manifest.get("session", {})
    if session:
        if not all(isinstance(session.get(k), str) and session[k].strip() for k in ("platform", "id")):
            raise ValueError("Session identity needs platform and stable id")
        if registry.get("version", 1) >= 3:
            previous = [s for s in registry["sources"] if s.get("session", {}).get("platform") == session["platform"]
                        and s.get("session", {}).get("id") == session["id"]]
            expected = previous[-1]["sha256"] if previous else None
            if session.get("previous_source_sha256") != expected:
                raise ValueError("Session cursor changed; inspect its last committed increment")
    source_name = source_id + "_" + source.name
    snapshot = ARCHIVE + '/originals/increments/' + source_name
    if safe(root, snapshot).exists():
        raise ValueError("Snapshot collision")
    actions = []
    for route in manifest["routes"]:
        topic = next((t for t in registry["topics"] if t["id"] == route["topic_id"]), None)
        if topic is None:
            confirmed_structure(manifest)
            definition = route.get("new_topic")
            if not definition or any(t["title"] == definition["title"] for t in registry["topics"]):
                raise ValueError("New topic needs a unique title and reviewed scope")
            validate_definition(definition, projects, registry["topics"] + [a["topic"] for a in actions])
            topic = {"id": route["topic_id"], **definition, "ranges": [], "last_conversation_date": ""}
            old = (f"# {topic['title']}\n\n- 主题 ID：`{topic['id']}`\n- 归档范围：{topic['scope']}\n").encode()
            old_path = None
        else:
            if route.get("new_matter"):
                confirmed_structure(manifest)
            if topic.get("version", "original") != "original":
                raise ValueError("Derived summaries are not dialogue append targets")
            old_path = safe(root, topic["path"])
            old = old_path.read_bytes()
            if route.get("base_sha256") != sha(old) or topic["sha256"] != sha(old):
                raise ValueError("Topic changed; refresh its scope and base hash")
        chosen = reviewed_units(record, data, route)
        dated = [u.get("occurred", unit_dates[u.get("parent_unit_id", u["id"])]) for u in chosen]
        occurred = max(dated)
        if route.get("last_conversation_date") != occurred:
            raise ValueError("Route date differs from actual message dates")
        last = max(occurred, topic["last_conversation_date"])
        target = ARCHIVE + '/' + last + "_" + topic["title"] + "_original.md"
        if safe(root, target).exists() and safe(root, target) != old_path:
            raise ValueError("Topic target collision")
        payload = bytearray(old)
        for unit, day in zip(chosen, dated):
            payload.extend(f"\n\n<!-- BEGIN_TOPIC_ORIGINAL: {source_id}:{unit['id']} | {day} | {snapshot} | L{unit['line']}-{unit['end_line']} -->\n".encode())
            start = len(payload)
            payload.extend(data[unit["start"]:unit["end"]])
            end = len(payload)
            payload.extend(f"\n<!-- END_TOPIC_ORIGINAL: {source_id}:{unit['id']} -->\n".encode())
            topic["ranges"].append({k: unit[k] for k in ("start", "end", "line", "end_line", "sha256")} |
                {"id": source_id + ":" + unit["id"], "source": snapshot, "source_id": source_id,
                 "occurred": day, "date_basis": "reviewed_paragraph_message_time" if "parent_unit_id" in unit else "full_local_message_heading",
                 "target_start": start, "target_end": end})
        payload.extend(f"\n\n<!-- TOPIC_APPEND_METADATA: latest_conversation={last}; topic={topic['id']} -->\n".encode())
        if bytes(payload[:len(old)]) != old:
            raise ValueError("Old topic bytes changed")
        actions.append({"topic": topic, "old_path": old_path, "old": old,
                        "target": target, "payload": bytes(payload), "last": last})
    summary = {"applied": apply, "source": manifest["source"], "routes": [
        {"topic_id": a["topic"]["id"], "target": a["target"], "date": a["last"]} for a in actions]}
    reference_changes, reference_guards = [], []
    if registry.get("version", 1) >= 3:
        from regroup_archive import find_references
        from archive_references import reviewed_changes
        renamed = [{"old": a["old_path"].relative_to(root).as_posix(), "new": a["target"]}
                   for a in actions if a["old_path"] and a["old_path"] != safe(root, a["target"])]
        hits = find_references(root, {"topics": [{"path": x["old"]} for x in renamed]}) if renamed else []
        reference_changes, reference_guards = reviewed_changes(root, renamed, manifest.get("references", []), hits)
        summary["references"] = hits
    if not apply:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    if source.read_bytes() != data or safe(root, REGISTRY).read_bytes() != registry_bytes:
        raise ValueError("Concurrent source or registry change; refresh the route")
    submitted_at = timestamp()
    today = submitted_at[:10]
    changes = [{"path": snapshot, "before": None, "after": data}] + reference_changes
    for a in actions:
        if a["old_path"] and a["old_path"].read_bytes() != a["old"]:
            raise ValueError("Concurrent topic append")
    for a in actions:
        topic = a["topic"]
        if a["old_path"]:
            old_name = a["old_path"].name
            registry["aliases"][old_name] = [topic["id"]]
            if a["old_path"] != safe(root, a["target"]):
                changes.append({"path": a["old_path"].relative_to(root).as_posix(),
                                "before": sha(a["old"]), "after": None})
        else:
            registry["topics"].append(topic)
        changes.append({"path": a["target"], "before": sha(a["old"]) if a["old_path"] == safe(root, a["target"]) else None,
                        "after": a["payload"]})
        topic.update({"path": a["target"], "last_conversation_date": a["last"], "sha256": sha(a["payload"])})
        if a["topic"]["id"] in {r["topic_id"] for r in manifest["routes"] if r.get("handoff")}:
            topic["handoff"] = next(r["handoff"] for r in manifest["routes"] if r["topic_id"] == topic["id"])
    registry["sources"].append({"id": source_id, "filename": source_name, "path": snapshot,
        "sha256": sha(data), "bytes": len(data), "topic_ids": [a["topic"]["id"] for a in actions],
        "submitted_at": submitted_at, "actor": manifest.get("actor", "未记录"), "session": session,
        "already_archived": skipped, "repeat_units": manifest.get("repeat_units", [])})
    if projects:
        registry["projects"] = projects
    registry["revision"] = registry.get("revision", 0) + 1
    registry.setdefault("events", []).append({"id": source_id, "type": "append", "at": submitted_at,
        "actor": manifest.get("actor", "未记录"), "source_id": source_id,
        "topic_ids": [a["topic"]["id"] for a in actions], "decision": manifest.get("decision", "已审阅的主题增量续接"),
        "confirmation": manifest.get("structure_confirmation"), "routes": summary["routes"]})
    from archive_navigation import navigation_bytes
    nav = ARCHIVE + '/README.md'
    changes.extend([{"path": REGISTRY, "before": sha(registry_bytes), "after": json_bytes(registry)},
                    {"path": nav, "before": current_hash(safe(root, nav)), "after": navigation_bytes(registry)},
                    {"path": manifest["source"], "before": sha(data), "after": None},
                    {"path": BASE + "/" + today + "_主题追加_" + sha(data)[:16] + ".json",
                     "before": None, "after": json_bytes(summary)}])
    guards = reference_guards + [{"path": source_lookup[x["source_id"]]["path"], "sha256": source_lookup[x["source_id"]]["sha256"]} for x in skipped]
    transaction = commit_locked(root, changes, "主题追加", guards=guards)
    summary["transaction_id"] = transaction["id"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def scan(path, ident):
    data = path.read_bytes()
    lines = physical_lines(data)
    offsets, speakers, metadata = [], [], []
    offset = 0
    context = ""
    for n, raw in enumerate(lines, 1):
        line = raw.decode("utf-8-sig").rstrip("\r\n")
        offsets.append(offset)
        match = SPEAKER.match(line)
        if match:
            speakers.append({"line": n, "start": offset, "heading": line,
                             "role": match["role"], "date_context": context})
        if (re.match(r"^[-*] (?:对话时间|对话日期|覆盖日期)[：:]", line)
                or re.match(r"^#{1,3} (?:追加|续接|.*(?:20\d{2}-\d{2}-\d{2}).*(?:追加|续接|续更|补记))", line)
                or line.startswith("<!-- BEGIN_") or line.startswith("<!-- START_")):
            metadata.append({"line": n, "text": line[:500]})
            dates = DATE.findall(line)
            if dates:
                context = line
        offset += len(raw)
    starts = [s for s in speakers if s["role"] in ("User", "用户", "User")]
    units = []
    for i, item in enumerate(starts):
        end = starts[i + 1]["start"] if i + 1 < len(starts) else len(data)
        chunk = data[item["start"]:end]
        next_speaker = next((s["start"] for s in speakers
                             if item["start"] < s["start"] < end), end)
        question = data[item["start"]:next_speaker].decode("utf-8-sig")
        body = chunk.decode("utf-8-sig")
        units.append({"id": f"{ident}:{i + 1:04}", **item, "end": end,
                      "end_line": data[:end].count(b"\n") + int(not data[:end].endswith(b"\n")),
                      "sha256": sha(chunk), "question": question,
                      "response_headings": re.findall(r"(?m)^#{1,4} [^\r\n]+", body)[1:]})
    return {"id": ident, "filename": path.name, "bytes": len(data), "sha256": sha(data),
            "units": units, "metadata": metadata, "speaker_count": len(speakers)}


def initialize(root):
    from archive_navigation import navigation_bytes
    root.mkdir(parents=True, exist_ok=True)
    with writer_lock(root):
        archive = safe(root, ARCHIVE)
        if safe(root, REGISTRY).exists() or (archive.exists() and any(archive.iterdir())):
            raise ValueError("Archive already exists; inspect it rather than initializing over it")
        registry = {"version": 3, "revision": 0, "projects": [], "topics": [],
                    "sources": [], "aliases": {}, "events": [], "prior_views": []}
        (archive / "incoming").mkdir(parents=True)
        (archive / "originals").mkdir()
        safe(root, ".trash").mkdir(exist_ok=True)
        result = commit_locked(root, [
            {"path": REGISTRY, "before": None, "after": json_bytes(registry)},
            {"path": ARCHIVE + "/README.md", "before": None, "after": navigation_bytes(registry)}
        ], "Initialize local original-dialogue handoff")
        print(json.dumps({"initialized": ARCHIVE, "registry": REGISTRY,
                          "transaction": result["id"]}))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    p = sub.add_parser("resolve")
    p.add_argument("query")
    sub.add_parser("verify")
    sub.add_parser("navigation")
    sub.add_parser("recover")
    p = sub.add_parser("inspect-file")
    p.add_argument("source")
    p.add_argument("--output", required=True)
    p = sub.add_parser("route")
    p.add_argument("manifest")
    p.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "init":
        initialize(root)
    elif args.command == "resolve":
        resolve(root, args.query)
    elif args.command == "verify":
        verify(root)
    elif args.command == "recover":
        print(json.dumps({"recovered": [j["id"] for j in recover(root)]}))
    elif args.command == "navigation":
        from archive_navigation import navigation_bytes
        with writer_lock(root):
            registry = read_json(safe(root, REGISTRY))
            path = ARCHIVE + "/README.md"
            commit_locked(root, [{"path": path, "before": current_hash(safe(root, path)),
                                  "after": navigation_bytes(registry)}], "Refresh handoff catalog")
    elif args.command == "inspect-file":
        record = scan(safe(root, args.source), "INCOMING")
        output = safe(root, args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
        print(json.dumps({"units": len(record["units"]), "sha256": record["sha256"]}))
    elif args.command == "route":
        append_topics(root, read_json(safe(root, args.manifest)), args.apply)



if __name__ == "__main__":
    main()
