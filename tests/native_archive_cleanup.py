"""One synthetic model turn verifies the installed native SessionStart callback."""

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import archive_cleanup
from memory_store import Store, atomic_write, empty_checkpoint, read_json


class TestClient(archive_cleanup.ArchiveClient):
    def __init__(self, binary, log):
        self.events = []
        self.deadline = time.monotonic() + 180
        self.seq = 0
        self.inbox = queue.Queue()
        env = dict(os.environ)
        env.pop("CODEX_THREAD_ID", None)
        self.proc = subprocess.Popen(
            [binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
            encoding="utf-8",
            env=env,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                message = json.loads(line)
                if "method" in message:
                    self.events.append(message)
                self.inbox.put(message)
        finally:
            self.inbox.put({"transport_error": "App-server closed its output"})

    def wait_turn(self, thread, turn):
        while time.monotonic() < self.deadline:
            for event in self.events:
                p = event.get("params", {})
                if (
                    event.get("method") == "turn/completed"
                    and p.get("threadId") == thread
                    and p.get("turn", {}).get("id") == turn
                ):
                    assert p["turn"]["status"] == "completed", event
                    return
            time.sleep(0.05)
        raise TimeoutError("Native test turn timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--work-dir", required=True)
    args = parser.parse_args()
    store = Store(args.vault)
    base = Path(args.work_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    binary = store.config["codex_bin"]
    log = (base / "server.log").open("w")
    client = TestClient(binary, log)
    threads, archived_ids, report = [], set(), {}
    try:
        client.call(
            "initialize",
            {
                "clientInfo": {"name": "preserve-context-archive-test", "version": "1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        client.send({"method": "initialized", "params": {}})
        listed = client.call("hooks/list", {"cwds": [str(base)]})
        own = [h for h in listed["data"][0]["hooks"] if "preserve-context/scripts/hooks.py" in h.get("command", "")]
        assert len(own) == 2 and all(h["enabled"] and h["trustStatus"] == "trusted" for h in own), own
        common = {
            "cwd": str(base),
            "model": store.config.get("model"),
            "sandbox": "read-only",
            "ephemeral": False,
            "developerInstructions": "Isolated memory-hook integration test. Do not use tools or skills. Answer only OK. Hooks handle memory.",
            "config": {"project_doc_max_bytes": 0, "features.memories": False, "model_reasoning_effort": "low"},
        }
        archived = client.call("thread/start", common)["thread"]["id"]
        threads.append(archived)
        store.register(archived, base)
        cp = empty_checkpoint()
        cp["goal"] = "native-cleanup-check-" + archived
        store.save_agent(archived, {"checkpoint": cp, "updates": []})
        archived_state = store.load(archived)
        raw = store.state_dir(archived) / "archives/test-raw.jsonl"
        atomic_write(raw, "synthetic raw transcript")
        archived_state["pending"] = [{"archive": str(raw.relative_to(store.vault))}]
        store.snapshot(archived, archived_state)
        store.commit(archived, archived_state)
        client.call("thread/archive", {"threadId": archived})
        archived_ids.add(archived)
        before = len(client.events)
        active = client.call("thread/start", common)["thread"]["id"]
        threads.append(active)
        store.register(active, base)
        cp = empty_checkpoint()
        cp["goal"] = "native-cleanup-check-" + active
        store.save_agent(active, {"checkpoint": cp, "updates": []})
        active_before = store.load(active)
        turn = client.call(
            "turn/start",
            {
                "threadId": active,
                "input": [{"type": "text", "text": "Answer only OK.", "text_elements": []}],
                "effort": "low",
            },
        )["turn"]["id"]
        client.wait_turn(active, turn)
        runs = [
            e["params"]["run"]
            for e in client.events[before:]
            if e.get("method") == "hook/completed"
            and e["params"].get("threadId") == active
            and str(e["params"].get("run", {}).get("statusMessage", "")).startswith("preserve-context:")
        ]
        assert any(r["eventName"] == "sessionStart" and r["status"] == "completed" for r in runs), runs
        assert not store.task_dir(archived).exists()
        assert not (store.state_dir(archived) / "state.json").exists()
        assert not raw.exists()
        assert store.load(active) == active_before
        registry = read_json(store.root / "registry.json")["tasks"]
        assert archived not in registry and active in registry
        # The external-memory cleanup must leave the actual archived conversation.
        assert client.call("thread/read", {"threadId": archived, "includeTurns": False})["thread"]["id"] == archived
        report = {
            "status": "passed",
            "archived": archived,
            "active": active,
            "archived_external_memory_removed": True,
            "active_checkpoint_unchanged": True,
            "native_archived_conversation_kept": True,
            "hook_runs": [{"event": r["eventName"], "status": r["status"]} for r in runs],
        }
        print(json.dumps(report, indent=2), flush=True)
    finally:
        for tid in threads:
            if tid in archived_ids:
                continue
            try:
                client.call("thread/archive", {"threadId": tid})
            except Exception as error:
                print("Test thread archive failed: " + str(error), file=sys.stderr)
        (base / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        (base / "events.json").write_text(json.dumps(client.events, indent=2) + "\n")
        client.close()
        log.close()


if __name__ == "__main__":
    main()
