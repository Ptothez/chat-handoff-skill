"""Short local writer lock and recoverable, hash-checked archive transactions.

The lock coordinates processes on one filesystem, not separate synchronized replicas.
Prepared transactions retain before/after bytes; recovery rolls forward only when
every live path still equals its recorded before or after image.
"""
from archive_layout import ARCHIVE, BASE, REGISTRY, configured_zone, relative_from_archive, find_root
import hashlib
import json
import os
import shutil
import socket
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UNSPECIFIED = object()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def timestamp():
    return datetime.now().astimezone(configured_zone()).isoformat(timespec="seconds")


def checked(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Outside workspace: " + str(relative))
    return path


def current_hash(path):
    return digest(path.read_bytes()) if path.is_file() else None


def atomic_bytes(path, data, expected=UNSPECIFIED):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".prepared-" + uuid.uuid4().hex)
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    for attempt in range(6):
        if expected is not UNSPECIFIED and current_hash(path) != expected:
            raise ValueError("Concurrent publish change; retain prepared bytes: " + str(path))
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@contextmanager
def writer_lock(root, timeout=10):
    directory = checked(root, BASE)
    directory.mkdir(parents=True, exist_ok=True)
    # Never unlink the lock inode: another process may already be waiting on it.
    with (directory / "writer.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("Archive writer busy; refresh after the other submission")
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def pending(root):
    directory = checked(root, BASE + "/transactions")
    return [p for p in sorted(directory.glob("*/journal.json"))
            if json.loads(p.read_text(encoding="utf-8"))["state"] != "complete"]


def resume(root, journal_path, fail_after=None):
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal["state"] == "complete":
        return journal
    for guard in journal.get("guards", []):
        if current_hash(checked(root, guard["path"])) != guard["sha256"]:
            raise ValueError("Read dependency changed; retain evidence and review: " + guard["path"])
    for op in journal["operations"]:
        path = checked(root, op["path"])
        if current_hash(path) not in (op["before"], op["after"]):
            raise ValueError("Recovery conflict; retain evidence and review: " + op["path"])
        for key, expected in (("before_image", op["before"]), ("after_image", op["after"])):
            if expected is not None and current_hash(checked(root, op[key])) != expected:
                raise ValueError("Transaction evidence changed: " + op[key])
    for index, op in enumerate(journal["operations"], 1):
        path = checked(root, op["path"])
        actual = current_hash(path)
        if actual != op["after"]:
            if actual != op["before"]:
                raise ValueError("Concurrent noncooperating writer: " + op["path"])
            if op["after"] is None:
                destination = checked(root, op["retired_path"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError("Retired path collision: " + op["retired_path"])
                shutil.move(str(path), str(destination))
            else:
                atomic_bytes(path, checked(root, op["after_image"]).read_bytes(), expected=op["before"])
        if current_hash(path) != op["after"]:
            raise ValueError("Transaction publish mismatch: " + op["path"])
        journal["completed_operations"] = index
        atomic_bytes(journal_path, json_bytes(journal))
        if fail_after == index:
            raise RuntimeError("Injected interruption")
    journal["state"] = "complete"
    journal["completed_at"] = timestamp()
    atomic_bytes(journal_path, json_bytes(journal))
    return journal


def commit_locked(root, changes, label, fail_after=None, guards=None):
    """Caller holds writer_lock; changes are path, before hash, after bytes/None."""
    if pending(root):
        raise ValueError("Pending archive transaction; run recover before another submission")
    if len({os.path.normcase(str(checked(root, c["path"]))) for c in changes}) != len(changes):
        raise ValueError("Duplicate transaction path")
    written = {c["path"] for c in changes}
    guards = [g for g in guards or [] if g["path"] not in written]
    for guard in guards:
        if current_hash(checked(root, guard["path"])) != guard["sha256"]:
            raise ValueError("Read dependency changed; refresh: " + guard["path"])
    for change in changes:
        target = checked(root, change["path"])
        if target.exists() and not target.is_file():
            raise ValueError("Transaction target is not a regular file")
        if change["before"] is None and change["after"] is None:
            raise ValueError("Cannot retire an absent file")
        if current_hash(target) != change["before"]:
            raise ValueError("Concurrent file change; refresh: " + change["path"])
    ident = timestamp()[:10] + "_" + uuid.uuid4().hex[:16]
    directory = checked(root, BASE + "/transactions/" + ident)
    trash = ".trash/" + ident + "_chat-handoff"
    directory.mkdir(parents=True)
    operations = []
    for index, change in enumerate(changes):
        op = {"path": change["path"], "before": change["before"],
              "after": digest(change["after"]) if change["after"] is not None else None}
        if op["before"] is not None:
            op["before_image"] = trash + "/before/" + change["path"]
            target = checked(root, op["before_image"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(checked(root, change["path"]), target)
            if current_hash(target) != op["before"]:
                raise ValueError("Concurrent change while preparing backup")
        if op["after"] is not None:
            op["after_image"] = (directory / (str(index) + ".after")).relative_to(root).as_posix()
            checked(root, op["after_image"]).write_bytes(change["after"])
        else:
            op["retired_path"] = trash + "/retired/" + change["path"]
        operations.append(op)
    ledger = checked(root, ".trash/README.md")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if any(c["path"] == ".trash/README.md" for c in changes):
        raise ValueError("Trash ledger is maintained by the transaction")
    with ledger.open("ab") as handle:
        handle.write((f"\n- `{trash}/`：{label}的提交前字节与退役入口；"
                      "Moved to recoverable trash; clean only with explicit user approval and after checking dependencies.\n").encode("utf-8"))
    journal = {"id": ident, "label": label, "state": "prepared", "prepared_at": timestamp(),
               "host": socket.gethostname(), "pid": os.getpid(), "completed_operations": 0,
               "operations": operations, "guards": guards}
    path = directory / "journal.json"
    atomic_bytes(path, json_bytes(journal))
    return resume(root, path, fail_after)


def recover(root):
    with writer_lock(root):
        return [resume(root, p) for p in pending(root)]
