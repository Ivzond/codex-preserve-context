"""Delete vault data only for threads positively listed as archived by Codex."""

import argparse
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

from memory_store import Store, atomic_write, lock, read_json, write_json

# An empty/omitted sourceKinds filter includes only interactive CLI/IDE threads.
SOURCE_KINDS = [
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]


class ArchiveClient:
    """Bounded local metadata requests. Never resumes a thread or calls a model."""

    def __init__(self, binary, timeout=6):
        self.deadline = time.monotonic() + timeout
        self.seq = 0
        self.inbox = queue.Queue()
        env = dict(os.environ)
        env.pop("CODEX_THREAD_ID", None)
        self.proc = subprocess.Popen(
            [binary, "-c", "features.hooks=false", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=env,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                try:
                    self.inbox.put(json.loads(line))
                except ValueError:
                    self.inbox.put({"transport_error": "Invalid app-server JSON"})
        finally:
            self.inbox.put({"transport_error": "App-server closed its output"})

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def call(self, method, params):
        self.seq += 1
        rid = self.seq
        self.send({"id": rid, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Archived-thread lookup timed out")
            try:
                reply = self.inbox.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("Archived-thread lookup timed out")
            if not isinstance(reply, dict) or "transport_error" in reply:
                raise RuntimeError("Archived-thread lookup transport failed")
            if "method" in reply:
                if "id" in reply:
                    # Metadata calls must never require tool execution/approval.
                    raise RuntimeError("Unexpected app-server request during archive lookup")
                continue
            if reply.get("id") != rid:
                continue
            if "error" in reply:
                raise RuntimeError("App-server rejected " + method)
            result = reply.get("result")
            if not isinstance(result, dict):
                raise ValueError("Malformed " + method + " result")
            return result

    def archived(self, targets):
        self.call("initialize", {"clientInfo": {"name": "preserve-context-cleanup", "version": "1.0"}})
        self.send({"method": "initialized", "params": {}})
        found, cursors, cursor = set(), set(), None
        for _ in range(200):
            params = {"archived": True, "limit": 100, "sourceKinds": SOURCE_KINDS, "useStateDbOnly": True}
            if cursor is not None:
                params["cursor"] = cursor
            page = self.call("thread/list", params)
            rows = page.get("data")
            if not isinstance(rows, list) or "nextCursor" not in page:
                raise ValueError("Malformed archived-thread page")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    raise ValueError("Malformed archived-thread identity")
                if row["id"] in targets:
                    found.add(row["id"])
            cursor = page["nextCursor"]
            if cursor is None or found == targets:
                return found
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError("Invalid archived-thread pagination")
            cursors.add(cursor)
        raise RuntimeError("Archived-thread lookup exceeded its page budget")

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=0.3)
        self.proc.stdin.close()
        self.proc.stdout.close()


def archived_threads(store, targets, timeout):
    binary = store.config.get("codex_bin")
    if not isinstance(binary, str) or not binary:
        raise ValueError("Codex CLI is not configured for archive cleanup")
    client = ArchiveClient(binary, timeout)
    try:
        return client.archived(targets)
    finally:
        client.close()


def remove_promotions(store, task, identity):
    """Remove only this task's generated promotion blocks; keep other prose."""
    project = identity.get("project")
    store.check_task_id(project)
    path = store.vault / "projects" / project / "knowledge.md"
    if path.is_symlink() or (store.vault / "projects" / project).is_symlink():
        raise ValueError("Refusing to edit a linked project-knowledge file")
    with lock(store.root / "project-" / (project + ".lock"), timeout=0.1):
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        # The old format ended at its exact Source footer. The current format
        # also has an explicit closing marker, including after human edits.
        marker = r"\n<!-- promoted:" + re.escape(task) + r":([A-Za-z0-9_-]+) -->\n"
        start = re.compile(marker)
        while True:
            match = start.search(text)
            if not match:
                break
            entry = match.group(1)
            end_marker = "<!-- /promoted:" + task + ":" + entry + " -->"
            next_start = text.find("\n<!-- promoted:", match.end())
            stop = len(text) if next_start < 0 else next_start
            end = text.find(end_marker, match.end(), stop)
            if end >= 0:
                end += len(end_marker)
                if text[end : end + 1] == "\n":
                    end += 1
            else:
                footer = "Source: [task " + task + "](../../tasks/" + task + "/findings.md#^" + entry + ")."
                end = text.find(footer, match.end(), stop)
                if end < 0:
                    raise ValueError("Task promotion has no recognizable boundary; cleanup deferred")
                end += len(footer)
                if text[end : end + 1] == "\n":
                    end += 1
            text = text[: match.start()] + text[end:]
        original = path.read_text(encoding="utf-8")
        if text != original:
            atomic_write(path, text)


def remove_path(path, deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("Cleanup time budget exhausted; retry at next SessionStart")
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    # Check between files too: one large task must not consume the entire
    # SessionStart timeout and prevent restoration of the active checkpoint.
    with os.scandir(path) as children:
        for child in children:
            remove_path(path / child.name, deadline)
    if time.monotonic() >= deadline:
        raise TimeoutError("Cleanup time budget exhausted; retry at next SessionStart")
    path.rmdir()


def remove_task(store, task, identity, deadline):
    store.check_task_id(task)
    notes, state_dir = store.task_dir(task), store.state_dir(task)
    if state_dir.is_symlink():
        raise ValueError("Refusing to traverse a linked task-state directory")
    with store.transaction(task, timeout=0.1):
        # Keep the existing lock inode so concurrent/waiting writers continue
        # to coordinate. Its empty file contains no checkpoint or knowledge.
        remove_promotions(store, task, identity)
        if notes.is_symlink():
            remove_path(notes, deadline)
        elif notes.exists():
            remove_path(notes, deadline)
        children = sorted(state_dir.iterdir(), key=lambda p: p.name == "state.json")
        for child in children:
            if child.name == "task.lock":
                continue
            remove_path(child, deadline)
        with lock(store.root / "registry.lock", timeout=0.1):
            registry = read_json(store.root / "registry.json", {"version": 1, "tasks": {}})
            registry["tasks"].pop(task, None)
            write_json(store.root / "registry.json", registry)


def cleanup(store, current=None, lookup=None, dry_run=False):
    result = {"removed": [], "candidates": [], "errors": []}
    if not store.config.get("archive_cleanup_enabled", False):
        return dict(result, status="disabled")
    try:
        for root in (store.root, store.vault / "tasks", store.vault / "projects"):
            if root.is_symlink():
                raise ValueError("Refusing cleanup through a linked vault directory")
        # Do not queue slow cleanup behind another starting chat's collector.
        with lock(store.root / "archive-cleanup.lock", timeout=0.05):
            registry = read_json(store.root / "registry.json", {"version": 1, "tasks": {}})
            tasks = registry["tasks"]
            if not isinstance(tasks, dict):
                raise ValueError("Invalid memory registry")
            # Only concrete registered thread IDs. Never delete missing entries
            # or an unresolved/shared parent session based on absence/age.
            targets = {
                k
                for k, v in tasks.items()
                if k != current
                and isinstance(v, dict)
                and v.get("identity_kind") == "thread"
                and v.get("identity") == k
            }
            if not targets:
                return dict(result, status="ok")
            timeout = min(max(float(store.config.get("archive_cleanup_timeout_seconds", 6)), 0.1), 10)
            confirmed = (lookup or archived_threads)(store, targets, timeout)
            if not isinstance(confirmed, set) or any(k not in targets for k in confirmed):
                raise ValueError("Unexpected cleanup identities")
            result["candidates"] = sorted(confirmed)
            deadline = time.monotonic() + 6
            if not dry_run:
                for task in sorted(confirmed):
                    if time.monotonic() >= deadline:
                        result["errors"].append("Cleanup time budget exhausted; retry at next SessionStart")
                        break
                    try:
                        remove_task(store, task, tasks[task], deadline)
                        result["removed"].append(task)
                    except (OSError, ValueError, KeyError, TypeError, TimeoutError) as error:
                        result["errors"].append(task + ": " + str(error))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, TimeoutError) as error:
        result["errors"].append(str(error))
    return dict(result, status="deferred" if result["errors"] else "ok")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(cleanup(Store(args.vault), dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
