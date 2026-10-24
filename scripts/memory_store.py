#!/usr/bin/env python3
"""Crash-recoverable memory store. Python 3.9+, standard library, macOS/Linux."""

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone

SKILL_DIR = Path(__file__).resolve().parent.parent
CHECKPOINT_FIELDS = {
    "goal": str,
    "status": str,
    "constraints": list,
    "completed": list,
    "current_question": str,
    "working_hypotheses": list,
    "next_actions": list,
    "open_questions": list,
    "running_operations": list,
    "important_refs": list,
    "user_corrections": list,
}
LABELS = {
    "goal": "Goal",
    "constraints": "Constraints",
    "completed": "Completed / checks",
    "current_question": "Current question",
    "working_hypotheses": "Working hypotheses",
    "next_actions": "Next actions",
    "open_questions": "Open questions",
    "running_operations": "Running operations",
    "important_refs": "References / exact identifiers",
    "user_corrections": "User corrections",
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    if path.is_file() and not path.is_symlink() and path.read_bytes() == data:
        return
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path, default=None):
    if not Path(path).exists():
        return copy.deepcopy(default)
    return json.loads(Path(path).read_text(encoding="utf-8"))


@contextmanager
def lock(path, timeout=12):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Memory lock is busy: " + str(path))
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def safe_id(identity):
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,90}", identity):
        return identity
    return "t-" + digest(identity)[:24]


def empty_checkpoint():
    result = {k: ([] if typ is list else "") for k, typ in CHECKPOINT_FIELDS.items()}
    result["status"] = "active"
    return result


def validate_checkpoint(cp):
    if not isinstance(cp, dict) or set(cp) != set(CHECKPOINT_FIELDS):
        raise ValueError("Checkpoint must contain exactly: " + ", ".join(CHECKPOINT_FIELDS))
    for key, typ in CHECKPOINT_FIELDS.items():
        if not isinstance(cp[key], typ):
            raise ValueError("Wrong checkpoint field type: " + key)
        if typ is list and (len(cp[key]) > 50 or any(not isinstance(x, str) for x in cp[key])):
            raise ValueError("Expected a short list of strings: " + key)
    if cp["status"] not in ("active", "blocked", "complete"):
        raise ValueError("Invalid task status")
    if len(canonical(cp)) > 6000:
        raise ValueError("Checkpoint exceeds 6000 characters; move details to entries")


def validate_result(result, state, allowed_sources):
    if not isinstance(result, dict) or set(result) != {"base_revision", "checkpoint", "checkpoint_sources", "updates"}:
        raise ValueError("Invalid summarizer result fields")
    if type(result["base_revision"]) is not int or result["base_revision"] != state["revision"]:
        raise ValueError("Stale memory revision")
    if result["checkpoint"] is not None:
        validate_checkpoint(result["checkpoint"])
    updates = result["updates"]
    if not isinstance(updates, list) or len(updates) > 100:
        raise ValueError("Too many updates / invalid updates")
    ids = {e["id"] for e in state["entries"]}

    def check_sources(values):
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(s, str) or s not in allowed_sources for s in values)
        ):
            raise ValueError("Missing or invented evidence reference")

    check_sources(result["checkpoint_sources"])
    for update in updates:
        if not isinstance(update, dict) or set(update) != {
            "kind",
            "title",
            "body",
            "confidence",
            "sources",
            "supersedes",
        }:
            raise ValueError("Invalid update fields")
        if update["kind"] not in ("finding", "decision", "journal") or update["confidence"] not in (
            "confirmed",
            "hypothesis",
            "unverified",
        ):
            raise ValueError("Invalid update category/confidence")
        if any(not isinstance(update[k], str) or not update[k].strip() for k in ("title", "body")):
            raise ValueError("Empty update")
        if len(update["title"]) > 200 or len(update["body"]) > 12000:
            raise ValueError("Update exceeds size limit")
        check_sources(update["sources"])
        if not isinstance(update["supersedes"], list) or any(s not in ids for s in update["supersedes"]):
            raise ValueError("Invalid superseded entry")


class Store:
    def __init__(self, vault=None):
        settings = read_json(SKILL_DIR / "settings.json", {})
        raw = vault or settings.get("vault")
        if not raw:
            raise ValueError("Vault is not configured; install the skill or supply --vault")
        self.vault = Path(raw).expanduser().resolve()
        self.root = self.vault / ".state"
        self.config = read_json(self.root / "config.json", {})

    def task_dir(self, task):
        self.check_task_id(task)
        return self.vault / "tasks" / task

    def state_dir(self, task):
        self.check_task_id(task)
        return self.root / task

    @staticmethod
    def check_task_id(task):
        if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,90}", task):
            raise ValueError("Unsafe task ID")

    def project_id(self, cwd):
        path = Path(cwd).resolve()
        home_root = Path.home().resolve()
        root = path
        for parent in (path,) + tuple(path.parents):
            if parent == home_root and parent != path:
                # ~/.arc is commonly a user configuration directory, not an
                # Arcadia mount; it must not combine every workspace at home.
                break
            if (parent / ".git").exists() or (parent / ".arc").exists():
                root = parent
                break
        # Separate projectless workspaces even when they share Documents/Codex.
        label = re.sub(r"[^a-zA-Z0-9_-]", "-", root.name)[:32] or "project"
        return label + "-" + digest(str(root))[:12], str(root)

    def register(self, identity, cwd, transcript=None, identity_kind="thread"):
        task = safe_id(identity)
        project, project_root = self.project_id(cwd)
        with lock(self.root / "registry.lock"):
            registry = read_json(self.root / "registry.json", {"version": 1, "tasks": {}})
            old = registry["tasks"].get(task)
            if old and old["identity"] != identity:
                raise ValueError("Task identity collision")
            if not old:
                old = {
                    "identity": identity,
                    "identity_kind": identity_kind,
                    "project": project,
                    "project_root": project_root,
                    "cwd": str(Path(cwd).resolve()),
                    "created": now(),
                }
            elif old.get("project_root") == str(Path.home().resolve()) and old.get("cwd") != str(Path.home().resolve()):
                old["project"], old["project_root"] = self.project_id(old["cwd"])
            if transcript:
                old["transcript_path"] = str(Path(transcript).resolve())
            registry["tasks"][task] = old
            write_json(self.root / "registry.json", registry)
        with self.transaction(task):
            state_path = self.state_dir(task) / "state.json"
            if not state_path.exists():
                state = {
                    "version": 1,
                    "task_id": task,
                    "identity": old,
                    "revision": 0,
                    "updated": now(),
                    "checkpoint": empty_checkpoint(),
                    "checkpoint_sources": [],
                    "entries": [],
                    "sources": {},
                    "cursor": {},
                    "pending": [],
                    "coverage": "not_captured",
                    "coverage_issues": [],
                }
                write_json(state_path, state)
            else:
                state = self.load(task)
                if state["identity"].get("project") != old["project"]:
                    self.snapshot(task, state)
                    state["identity"] = old
                    self.commit(task, state)
            self.render(self.load(task))
            journal = self.task_dir(task) / "journal.md"
            if not journal.exists():
                atomic_write(
                    journal,
                    "---\nkind: human-journal\ntask_id: "
                    + json.dumps(task)
                    + "\n---\n\n# Journal\n\nAdd human notes here. This file is never overwritten by the summarizer.\n",
                )
        with lock(self.root / "project-" / (project + ".lock")):
            knowledge = self.vault / "projects" / project / "knowledge.md"
            if not knowledge.exists():
                atomic_write(
                    knowledge,
                    "---\nkind: project-knowledge\nproject_id: "
                    + json.dumps(project)
                    + "\n---\n\n# Project knowledge\n\nWorkspace: "
                    + project_root
                    + "\n\nConfirmed reusable findings can be promoted here. Human edits are retained.\n",
                )
        return task

    def transaction(self, task, timeout=12):
        return lock(self.state_dir(task) / "task.lock", timeout)

    def load(self, task):
        state = read_json(self.state_dir(task) / "state.json")
        if not state or state.get("version") != 1:
            raise ValueError("Unknown task / unsupported state version: " + task)
        return state

    def snapshot(self, task, state):
        dest = self.state_dir(task) / "snapshots" / (str(state["revision"]) + "-" + digest(canonical(state))[:16])
        dest.mkdir(parents=True, exist_ok=True)
        write_json(dest / "state.json", state)
        for name in ("resume.md", "findings.md", "decisions.md", "journal.md"):
            path = self.task_dir(task) / name
            if path.exists():
                atomic_write(dest / name, path.read_bytes())
        return dest

    def commit(self, task, state):
        state["revision"] += 1
        state["updated"] = now()
        # Single atomic source of truth includes checkpoint, entries AND cursor.
        write_json(self.state_dir(task) / "state.json", state)
        self.render(state)

    def apply_result(self, state, result, sources, cursor=None):
        validate_result(result, state, set(state["sources"]) | set(sources))
        if result["checkpoint"] is not None:
            state["checkpoint"] = result["checkpoint"]
            state["checkpoint_sources"] = result["checkpoint_sources"]
        state["sources"].update(sources)
        by_id = {e["id"]: e for e in state["entries"]}
        for update in result["updates"]:
            entry_id = "e-" + digest(canonical({k: update[k] for k in ("kind", "title", "body", "confidence")}))[:20]
            if entry_id in by_id:
                entry = by_id[entry_id]
                entry["sources"] = sorted(set(entry["sources"]) | set(update["sources"]))
                entry["supersedes"] = sorted(set(entry["supersedes"]) | set(update["supersedes"]))
            else:
                entry = dict(update, id=entry_id, created=now(), superseded_by=[])
                state["entries"].append(entry)
                by_id[entry_id] = entry
            for obsolete in update["supersedes"]:
                if obsolete != entry_id:
                    by_id[obsolete]["superseded_by"] = sorted(set(by_id[obsolete]["superseded_by"]) | {entry_id})
        if cursor is not None:
            state["cursor"] = cursor

    def _frontmatter(self, state, kind):
        return (
            "---\nkind: "
            + kind
            + "\ntask_id: "
            + json.dumps(state["task_id"])
            + "\nrevision: "
            + str(state["revision"])
            + "\nupdated: "
            + json.dumps(state["updated"])
            + "\nstatus: "
            + state["checkpoint"]["status"]
            + "\n---\n\n"
        )

    def _source_links(self, state, values):
        links = []
        for src in values:
            meta = state["sources"].get(src, {})
            path = meta.get("path")
            if path:
                relative = os.path.relpath(self.vault / path, self.task_dir(state["task_id"]))
                links.append("[" + src + "](<" + relative + ">)")
            else:
                links.append(src)
        return ", ".join(links)

    def render(self, state):
        task = state["task_id"]
        out = self._frontmatter(state, "checkpoint") + "# Resume\n\n"
        for key, label in LABELS.items():
            value = state["checkpoint"][key]
            if value:
                out += "## " + label + "\n\n"
                out += ("\n".join("- " + x for x in value) if isinstance(value, list) else value) + "\n\n"
        out += "Coverage: `" + state["coverage"] + "`. Pending archives: " + str(len(state["pending"])) + ".\n\n"
        if state["pending"]:
            out += "Resolve pending records before relying on affected assumptions.\n\n"
        if state.get("coverage_issues"):
            out += "Coverage issues (see evidence index): " + str(len(state["coverage_issues"])) + ".\n\n"
        out += "[Findings](findings.md) · [Decisions](decisions.md) · [Journal](journal.md) · [Evidence](evidence/index.md) · "
        out += "[Project knowledge](../../projects/" + state["identity"]["project"] + "/knowledge.md)\n"
        atomic_write(self.task_dir(task) / "resume.md", out)
        for kind, filename, title in (
            ("finding", "findings.md", "Findings"),
            ("decision", "decisions.md", "Decisions"),
        ):
            text = self._frontmatter(state, kind) + "# " + title + "\n\n"
            for entry in state["entries"]:
                if entry["kind"] not in (kind, "journal" if kind == "finding" else kind):
                    continue
                text += "## " + entry["title"] + "\n\n^" + entry["id"] + "\n\n"
                text += "Confidence: `" + entry["confidence"] + "`"
                if entry["superseded_by"]:
                    text += "; superseded by " + ", ".join(entry["superseded_by"])
                text += ".\n\n" + entry["body"] + "\n\nSources: " + self._source_links(state, entry["sources"]) + "\n\n"
            atomic_write(self.task_dir(task) / filename, text)
        text = "# Evidence index\n\nTask: " + task + "\n\n"
        for source, info in state["sources"].items():
            relative = os.path.relpath(self.vault / info["path"], self.task_dir(task) / "evidence")
            text += "- [" + source + "](<" + relative + ">) — " + info.get("kind", "source") + "\n"
        for pending in state["pending"]:
            relative = os.path.relpath(self.vault / pending["archive"], self.task_dir(task) / "evidence")
            text += "- PENDING [raw archive](<" + relative + ">): " + pending.get("reason", "not summarized") + "\n"
        for issue in state.get("coverage_issues", []):
            text += "- COVERAGE ISSUE: " + issue + "\n"
        atomic_write(self.task_dir(task) / "evidence" / "index.md", text)
        # A diagnostic mirror, never used as an independent transactional cursor.
        write_json(self.state_dir(task) / "cursor.json", state["cursor"])

    def save_agent(self, task, payload):
        with self.transaction(task):
            state = self.load(task)
            if "base_revision" in payload and payload["base_revision"] != state["revision"]:
                raise ValueError("Stale checkpoint; reload and merge first")
            patch = payload.get("checkpoint", {})
            if not isinstance(patch, dict) or not set(patch) <= set(CHECKPOINT_FIELDS):
                raise ValueError("Unknown checkpoint patch fields")
            checkpoint = dict(state["checkpoint"], **patch)
            validate_checkpoint(checkpoint)
            updates = payload.get("updates", [])
            if not isinstance(updates, list) or len(updates) > 100:
                raise ValueError("Too many updates / invalid updates")
            if checkpoint == state["checkpoint"] and not updates:
                return state
            source = "agent-" + digest(canonical(payload))[:24]
            path = self.task_dir(task) / "evidence" / (source + ".json")
            write_json(path, payload)
            sources = {
                source: {"path": str(path.relative_to(self.vault)), "kind": "agent checkpoint", "created": now()}
            }
            result = {
                "base_revision": state["revision"],
                "checkpoint": checkpoint,
                "checkpoint_sources": (
                    sorted(set(state["checkpoint_sources"]) | {source})
                    if len(patch) < len(CHECKPOINT_FIELDS)
                    else [source]
                ),
                "updates": [],
            }
            for update in updates:
                update = dict(update)
                update["sources"] = sorted(set(update.get("sources", [])) | {source})
                update.setdefault("supersedes", [])
                result["updates"].append(update)
            validate_result(result, state, set(state["sources"]) | {source})
            self.snapshot(task, state)
            self.apply_result(state, result, sources)
            self.commit(task, state)
            return state

    def promote(self, task, entry_id):
        with self.transaction(task):
            state = self.load(task)
            entry = next((e for e in state["entries"] if e["id"] == entry_id), None)
            if not entry or entry["confidence"] != "confirmed" or entry["superseded_by"]:
                raise ValueError("Only a current confirmed entry can be promoted")
            project = state["identity"]["project"]
            with lock(self.root / "project-" / (project + ".lock")):
                path = self.vault / "projects" / project / "knowledge.md"
                text = path.read_text(encoding="utf-8")
                marker = "<!-- promoted:" + task + ":" + entry_id + " -->"
                if marker not in text:
                    text += "\n" + marker + "\n\n## " + entry["title"] + "\n\n" + entry["body"]
                    text += "\n\nSource: [task " + task + "](../../tasks/" + task + "/findings.md#^" + entry_id + ").\n"
                    text += "<!-- /promoted:" + task + ":" + entry_id + " -->\n"
                    atomic_write(path, text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID"))
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--transcript")
    for name in ("save", "status", "render", "promote"):
        p = sub.add_parser(name)
        p.add_argument("--task", required=True)
        if name == "save":
            p.add_argument("--input", required=True)
        if name == "promote":
            p.add_argument("--entry", required=True)
    args = parser.parse_args()
    store = Store(args.vault)
    if args.command == "init":
        if not args.thread_id:
            raise ValueError("No concrete thread ID. Supply --thread-id; do not use a parent session ID")
        task = store.register(args.thread_id, args.cwd, args.transcript)
        result = {"task_id": task, "resume": str(store.task_dir(task) / "resume.md")}
    elif args.command == "save":
        import sys

        payload = json.loads(sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8"))
        state = store.save_agent(args.task, payload)
        result = {"task_id": args.task, "revision": state["revision"], "pending": len(state["pending"])}
    elif args.command == "promote":
        store.promote(args.task, args.entry)
        result = {"promoted": args.entry}
    else:
        with store.transaction(args.task):
            state = store.load(args.task)
            if args.command == "render":
                store.render(state)
        result = {k: state[k] for k in ("task_id", "revision", "coverage", "cursor", "pending")}
        result["resume"] = str(store.task_dir(args.task) / "resume.md")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TimeoutError, KeyError) as error:
        import sys

        print("preserve-context: " + str(error), file=sys.stderr)
        sys.exit(1)
