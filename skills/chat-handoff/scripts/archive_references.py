"""One-pass current-reference migration; historical evidence stays untouched."""
from archive_layout import ARCHIVE, BASE, REGISTRY, configured_zone, relative_from_archive, find_root
import json
import re
from pathlib import Path

from archive_store import checked, current_hash, digest


def replace_once(text, mapping):
    if not mapping:
        return text
    pattern = re.compile("|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)))
    def replacement(match):
        key = match.group(0)
        if "/" not in key and "\\" not in key:
            before = text[match.start() - 1] if match.start() else ""
            after = text[match.end()] if match.end() < len(text) else ""
            if (before and (before.isalnum() or before in "/\\_.-")) or (after and (after.isalnum() or after in "_.-")):
                return key
            if re.search(r"[A-Za-z]:$", text[:match.start()]):
                return key
        return mapping[key]
    return pattern.sub(replacement, text)


def rename_mapping(renames):
    mapping = {}
    for item in renames:
        for sep in ("/", "\\", "\\\\"):
            mapping[item["old"].replace("/", sep)] = item["new"].replace("/", sep)
        old_name, new_name = Path(item["old"]).name, Path(item["new"]).name
        if old_name in mapping and mapping[old_name] != new_name:
            raise ValueError("Ambiguous basename replacement; review a structured reference migration")
        mapping[old_name] = new_name
    return mapping


def replace_document(text, mapping, structured_json=False):
    if not structured_json:
        return replace_once(text, mapping)
    value = json.loads(text)
    def visit(item):
        if isinstance(item, str):
            return replace_once(item, mapping)
        if isinstance(item, list):
            return [visit(x) for x in item]
        if isinstance(item, dict):
            return {k: visit(v) for k, v in item.items()}
        return item
    return json.dumps(visit(value), ensure_ascii=False, indent=2) + "\n"


def reviewed_changes(root, renames, references, hits):
    if len({r["path"] for r in references}) != len(references) or set(hits) != {r["path"] for r in references}:
        raise ValueError("Review every rename reference hit: " + json.dumps(hits, ensure_ascii=False))
    mapping = rename_mapping(renames)
    changes, guards = [], []
    for ref in references:
        path = checked(root, ref["path"])
        data = path.read_bytes()
        if digest(data) != ref["sha256"] or ref.get("action") not in ("update", "historical"):
            raise ValueError("Reference changed or action unreviewed: " + ref["path"])
        if ref["action"] == "historical":
            if not ref.get("reason", "").strip():
                raise ValueError("Historical reference needs an evidence reason")
            guards.append({"path": ref["path"], "sha256": digest(data)})
            continue
        text = data.decode("utf-8-sig")
        after = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + replace_document(text, mapping, path.suffix == ".json").encode("utf-8")
        if after == data:
            raise ValueError("Reference hit not migrated; review its syntax: " + ref["path"])
        changes.append({"path": ref["path"], "before": digest(data), "after": after})
    return changes, guards
