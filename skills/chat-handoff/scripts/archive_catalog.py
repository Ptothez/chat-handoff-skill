"""Review and commit catalog metadata, byte-identical renames and legacy imports.

This command does not classify prose, rewrite chat bodies or split ranges. Use
regroup_archive for separately reviewed structural reassembly.
"""
from archive_layout import ARCHIVE, BASE, REGISTRY, configured_zone, relative_from_archive, find_root
import argparse
import copy
import json
import sys
from pathlib import Path

from archive_navigation import navigation_bytes
from archive_store import commit_locked, current_hash, json_bytes, pending, timestamp, writer_lock
from topic_archive import BASE, REGISTRY, confirmed_structure, read_json, safe, sha, validate_definition
from regroup_archive import find_references
from archive_references import reviewed_changes

FIELDS = {"title", "project_id", "kind", "scope", "stage_order", "related", "stage_basis", "handoff"}


def revise(root, plan, apply=False):
    with writer_lock(root):
        if pending(root):
            raise ValueError("Pending archive transaction; recover first")
        return revise_locked(root, plan, apply)


def revise_locked(root, plan, apply):
    confirmed_structure(plan)
    before = safe(root, REGISTRY).read_bytes()
    if sha(before) != plan["registry_sha256"]:
        raise ValueError("Registry changed; refresh the reviewed catalog plan")
    old = json.loads(before)
    registry = copy.deepcopy(old)
    topics = {t["id"]: t for t in registry["topics"]}
    changes, renamed, affected, guards = [], [], [], []
    if plan.get("projects") is not None:
        registry["projects"] = plan["projects"]
    projects = registry["projects"]
    if len({p["id"] for p in projects}) != len(projects) or len({p["prefix"] for p in projects}) != len(projects):
        raise ValueError("Duplicate project ID or prefix")
    if not all(all(isinstance(p.get(k), str) and p[k].strip() for k in ("id", "prefix", "goal", "current_line")) for p in projects):
        raise ValueError("Projects need a stated goal and current line")
    for update in plan.get("topics", []):
        topic = topics[update["id"]]
        if set(update) - FIELDS - {"id"}:
            raise ValueError("Catalog updates cannot edit original ranges, hashes or dates")
        previous = topic["path"]
        if current_hash(safe(root, previous)) != topic["sha256"]:
            raise ValueError("Current chat changed outside the registry: " + previous)
        guards.append({"path": previous, "sha256": topic["sha256"]})
        topic.update({k: v for k, v in update.items() if k != "id"})
        if topic.get("kind") != "mainline":
            topic.pop("stage_order", None)
        target = ARCHIVE + '/' + topic["last_conversation_date"] + "_" + topic["title"] + "_" + topic.get("version", "original") + ".md"
        if previous != target:
            data = safe(root, previous).read_bytes()
            if sha(data) != topic["sha256"] or safe(root, target).exists():
                raise ValueError("Changed original or rename collision")
            changes += [{"path": target, "before": None, "after": data},
                        {"path": previous, "before": sha(data), "after": None}]
            registry["aliases"][Path(previous).name] = [topic["id"]]
            topic["path"] = target
            renamed.append({"id": topic["id"], "old": previous, "new": target})
        affected.append(topic["id"])
    for spec in plan.get("imports", []):
        if spec["id"] in topics:
            raise ValueError("Import ID already exists")
        source = safe(root, spec["path"])
        if source.parent != safe(root, ARCHIVE):
            raise ValueError("Legacy import must be an existing active Markdown")
        data = source.read_bytes()
        if sha(data) != spec["sha256"] or b"\0" in data:
            raise ValueError("Legacy import changed")
        guards.append({"path": spec["path"], "sha256": sha(data)})
        if not spec.get("date_basis") or not spec["path"].split("/")[-1].startswith(spec["last_conversation_date"] + "_"):
            raise ValueError("Legacy import needs reviewed actual conversation date evidence")
        source_id = "legacy-" + sha(data)[:16]
        snapshot = ARCHIVE + '/originals/increments/' + source_id + "_" + source.name
        if any(s["sha256"] == sha(data) for s in registry["sources"]):
            raise ValueError("Legacy import already represented; review existing source mapping")
        span = {"id": source_id + ":whole", "source_id": source_id, "source": snapshot,
                "start": 0, "end": len(data), "line": 1, "end_line": data.count(b"\n") + 1,
                "sha256": sha(data), "target_start": 0, "target_end": len(data),
                "occurred": spec["last_conversation_date"], "date_basis": spec["date_basis"]}
        topic = {k: v for k, v in spec.items() if k not in ("date_basis", "actor", "submission_evidence")}
        topic["ranges"] = [span]
        registry["topics"].append(topic)
        topics[topic["id"]] = topic
        registry["sources"].append({"id": source_id, "filename": source.name, "path": snapshot,
            "sha256": sha(data), "bytes": len(data), "topic_ids": [topic["id"]],
            "actor": spec.get("actor", "未记录"), "imported_at": timestamp(),
            "submission_evidence": spec.get("submission_evidence", "仅登记原有文件，不猜首次录入时间")})
        changes.append({"path": snapshot, "before": None, "after": data})
        affected.append(topic["id"])
    for topic in registry["topics"]:
        validate_definition(topic, registry["projects"], [t for t in registry["topics"] if t["id"] != topic["id"]])
        if any(i not in topics for i in topic.get("related", [])):
            raise ValueError("Related ID missing")
    hits = find_references(root, {"topics": [t for t in old["topics"] if t["id"] in {x["id"] for x in renamed}]}) if renamed else []
    ref_changes, ref_guards = reviewed_changes(root, renamed, plan.get("references", []), hits)
    changes += ref_changes
    guards += ref_guards
    for metadata in plan.get("source_metadata", []):
        source = next(s for s in registry["sources"] if s["id"] == metadata["id"])
        if not metadata.get("basis") or set(metadata) - {"id", "actor", "submitted_at", "basis"}:
            raise ValueError("History enrichment needs evidence and cannot edit source identity")
        source.update({k: v for k, v in metadata.items() if k != "id"})
    registry["version"] = 3
    registry["revision"] = registry.get("revision", 0) + 1
    registry.setdefault("events", []).append({"id": "catalog-" + str(registry["revision"]), "type": "catalog-review",
        "at": timestamp(), "actor": plan["actor"], "topic_ids": affected,
        "decision": plan["decision"], "confirmation": plan["structure_confirmation"], "renames": renamed})
    nav = ARCHIVE + '/README.md'
    changes += [{"path": REGISTRY, "before": sha(before), "after": json_bytes(registry)},
                {"path": nav, "before": current_hash(safe(root, nav)), "after": navigation_bytes(registry)}]
    result = {"applied": apply, "reviewed": len(affected), "topics": len(registry["topics"]),
              "renames": renamed, "references": hits, "sources": len(registry["sources"])}
    if apply:
        result["transaction"] = commit_locked(root, changes, "聊天交接目录审阅与归属纠正", guards=guards)["id"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("plan")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    revise(args.root.resolve(), read_json(safe(args.root.resolve(), args.plan)), args.apply)
