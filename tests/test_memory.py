"""Deterministic fault/transaction tests; no model calls or global mutations."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hooks
import install
import memory_store as memory
import summarize
import transcript


def row(typ, payload):
    return (json.dumps({"type": typ, "payload": payload}, ensure_ascii=False) + "\n").encode("utf-8")


def message(text, role="user"):
    return row("response_item", {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]})


def fake_summary(store, task, state, records, model=None, timeout=None):
    cp = copy.deepcopy(state["checkpoint"])
    users = [r["text"] for r in records if r["kind"] == "user"]
    if users:
        cp["goal"] = users[-1][:700]
        cp["next_actions"] = ["Run exact test: check --id job-42"]
    cp["current_question"] = "Verify the retry branch"
    sources = {r["id"]: {"path": r["archive"], "kind": r["kind"]} for r in records}
    updates = [
        {
            "kind": "finding",
            "title": "Record " + r["id"],
            "body": r["text"],
            "confidence": "confirmed" if r["kind"] == "user" else "unverified",
            "sources": [r["id"]],
            "supersedes": [],
        }
        for r in records
        if len(r["text"]) <= 12000
    ]
    return {
        "base_revision": state["revision"],
        "checkpoint": cp,
        "checkpoint_sources": [r["id"] for r in records],
        "updates": updates,
    }, sources


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = memory.Store(self.base / "vault")
        self.path = self.base / "rollout.jsonl"
        self.path.write_bytes(
            row("session_meta", {"id": "thread-a", "session_id": "root", "cwd": str(self.base)})
            + message("Investigate operation job-42; keep exact ID")
        )
        self.payload = {
            "cwd": str(self.base),
            "session_id": "root",
            "transcript_path": str(self.path),
            "trigger": "manual",
        }
        self.task = self.store.register("thread-a", self.base, self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, summarizer=fake_summary):
        return hooks.precompact(self.store, self.payload, summarizer)

    def test_backup_checkpoint_restore_and_idempotence(self):
        self.assertTrue(self.run_hook()["continue"])
        state = self.store.load(self.task)
        self.assertEqual(state["coverage"], "complete_available")
        self.assertEqual(state["cursor"]["offset"], self.path.stat().st_size)
        restored = hooks.session_start(self.store, dict(self.payload, source="compact"))
        self.assertIn("job-42", restored["hookSpecificOutput"]["additionalContext"])
        self.assertIn("check --id job-42", restored["hookSpecificOutput"]["additionalContext"])
        count = len(state["entries"])
        with patch.object(summarize, "run", side_effect=AssertionError("must not call model")):
            self.assertTrue(hooks.precompact(self.store, self.payload)["continue"])
        self.assertEqual(len(self.store.load(self.task)["entries"]), count)

    def test_main_save_does_not_advance_summary_cursor(self):
        cp = memory.empty_checkpoint()
        cp["goal"] = "Agent checkpoint"
        self.store.save_agent(self.task, {"checkpoint": cp, "updates": []})
        self.assertEqual(self.store.load(self.task)["cursor"], {})
        self.run_hook()
        self.assertIn("job-42", self.store.load(self.task)["checkpoint"]["goal"])

    def test_late_correction_and_multiple_compactions_same_turn(self):
        self.run_hook()
        with self.path.open("ab") as f:
            f.write(message("Correction: operation job-77 replaces job-42"))
        self.payload["turn_id"] = "same-turn"
        self.run_hook()
        state = self.store.load(self.task)
        self.assertIn("job-77", state["checkpoint"]["goal"])
        self.assertTrue(any("job-42" in e["body"] for e in state["entries"]))
        self.assertEqual(state["pending"], [])

    def test_timeout_retains_cursor_raw_and_pending(self):
        old = self.store.load(self.task)["cursor"]

        def fail(*args):
            raise TimeoutError("fake timeout")

        result = self.run_hook(fail)
        self.assertTrue(result["continue"])
        state = self.store.load(self.task)
        self.assertEqual(state["cursor"], old)
        self.assertTrue(state["pending"])
        self.assertEqual((self.store.vault / state["pending"][0]["archive"]).read_bytes(), self.path.read_bytes())
        self.assertIn(
            "PENDING RAW RECORDS",
            hooks.session_start(self.store, self.payload)["hookSpecificOutput"]["additionalContext"],
        )

    def test_invalid_output_or_invented_sources_never_committed(self):
        def invalid(store, task, state, records, model=None, timeout=None):
            result, sources = fake_summary(store, task, state, records)
            result["checkpoint_sources"] = ["nonexistent-record"]
            return result, sources

        self.assertTrue(self.run_hook(invalid)["continue"])
        state = self.store.load(self.task)
        self.assertEqual(state["cursor"], {})
        self.assertEqual(state["entries"], [])
        self.assertIn("invented", state["pending"][0]["reason"])

    def test_raw_write_failure_explicitly_stops_compaction(self):
        with patch.object(transcript, "atomic_write", side_effect=OSError("disk full")):
            result = self.run_hook()
        self.assertFalse(result["continue"])
        self.assertIn("disk full", result["stopReason"])

    def test_missing_transcript_marks_incomplete_not_success(self):
        self.payload["transcript_path"] = None
        self.payload["thread_id"] = "thread-a"
        result = self.run_hook()
        self.assertTrue(result["continue"])
        state = self.store.load(self.task)
        self.assertEqual(state["coverage"], "incomplete")
        self.assertEqual(state["cursor"], {})

    def test_forks_with_shared_session_id_are_isolated(self):
        self.run_hook()
        fork = self.base / "fork.jsonl"
        fork.write_bytes(row("session_meta", {"id": "thread-b", "session_id": "root"}) + message("Different task B"))
        hooks.precompact(self.store, dict(self.payload, transcript_path=str(fork)), fake_summary)
        self.assertNotEqual(
            self.store.load("thread-a")["checkpoint"]["goal"], self.store.load("thread-b")["checkpoint"]["goal"]
        )

    def test_unknown_format_is_archived_and_marked_partial(self):
        with self.path.open("ab") as f:
            f.write(row("future_record", {"finding": "exact-id-999"}))
        self.run_hook()
        state = self.store.load(self.task)
        self.assertEqual(state["coverage"], "partial")
        self.assertTrue(any("exact-id-999" in e["body"] for e in state["entries"]))
        self.run_hook()
        self.assertEqual(self.store.load(self.task)["coverage"], "partial")

    def test_partial_last_line_then_append_recovers(self):
        tail = message("Correction arrives in fragments; job-81")
        with self.path.open("ab") as f:
            f.write(tail[:25])
        self.run_hook()
        self.assertTrue(self.store.load(self.task)["pending"])
        with self.path.open("ab") as f:
            f.write(tail[25:])
        self.run_hook()
        state = self.store.load(self.task)
        self.assertEqual(state["pending"], [])
        self.assertIn("job-81", state["checkpoint"]["goal"])

    def test_recovery_survives_deleted_original_transcript(self):
        def fail(*args):
            raise RuntimeError("network down")

        self.run_hook(fail)
        self.path.unlink()
        with patch.object(summarize, "run", side_effect=fake_summary):
            result = hooks.recover(self.store, self.task, force=True)
        self.assertTrue(result["continue"])
        self.assertEqual(result["pending"], 0)
        self.assertIn("job-42", self.store.load(self.task)["checkpoint"]["goal"])

    def test_replaced_transcript_does_not_discard_old_unprocessed_archive(self):
        def fail(*args):
            raise RuntimeError("network down")

        self.run_hook(fail)
        self.path.write_bytes(row("session_meta", {"id": "thread-a"}) + message("New transcript, job-91"))
        self.run_hook()
        state = self.store.load(self.task)
        self.assertTrue(any("job-42" in e["body"] for e in state["entries"]))
        self.assertIn("job-91", state["checkpoint"]["goal"])
        self.assertEqual(state["pending"], [])

    def test_superseding_keeps_prior_fact_and_evidence(self):
        cp = memory.empty_checkpoint()
        cp["goal"] = "correct fact"
        update = {"kind": "finding", "title": "Default", "body": "Default is 10", "confidence": "confirmed"}
        first = self.store.save_agent(self.task, {"checkpoint": cp, "updates": [update]})
        old_id = first["entries"][0]["id"]
        update = dict(update, body="Default is 20", supersedes=[old_id])
        state = self.store.save_agent(self.task, {"checkpoint": cp, "updates": [update]})
        self.assertEqual(len(state["entries"]), 2)
        self.assertTrue(state["entries"][0]["superseded_by"])
        self.assertTrue((self.store.task_dir(self.task) / "findings.md").read_text().count("Default is") == 2)

    def test_stale_main_save_refused(self):
        cp = memory.empty_checkpoint()
        cp["goal"] = "A changed checkpoint"
        self.store.save_agent(self.task, {"checkpoint": cp})
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.store.save_agent(self.task, {"base_revision": 0, "checkpoint": cp})

    def test_render_repair_and_human_journal_preservation(self):
        self.run_hook()
        journal = self.store.task_dir(self.task) / "journal.md"
        journal.write_text("Human correction: preserve me", encoding="utf-8")
        resume = self.store.task_dir(self.task) / "resume.md"
        resume.write_text("interrupted rendering", encoding="utf-8")
        hooks.session_start(self.store, self.payload)
        self.assertIn("job-42", resume.read_text())
        self.assertEqual(journal.read_text(), "Human correction: preserve me")

    def test_large_record_fragments_cursor_never_skips_remaining_text(self):
        self.path.write_bytes(row("session_meta", {"id": "thread-a"}) + message("A" * 16000 + "B" * 16000 + "C" * 3000))
        self.store.config["max_input_chars"] = 20000
        seen = []

        def fail_second(store, task, state, records, model=None, timeout=None):
            seen.extend(r["text"] for r in records)
            if len(seen) > 1:
                raise RuntimeError("interrupted second fragment")
            cp = memory.empty_checkpoint()
            cp["goal"] = "Long record"
            return {
                "base_revision": state["revision"],
                "checkpoint": cp,
                "checkpoint_sources": [records[0]["id"]],
                "updates": [],
            }, {records[0]["id"]: {"path": records[0]["archive"], "kind": "user"}}

        self.run_hook(fail_second)
        state = self.store.load(self.task)
        self.assertGreater(state["cursor"].get("fragment", 0), 0)
        recovered = []

        def finish(store, task, state, records, model=None, timeout=None):
            recovered.extend(r["text"] for r in records)
            cp = memory.empty_checkpoint()
            cp["goal"] = "Long record"
            return {
                "base_revision": state["revision"],
                "checkpoint": cp,
                "checkpoint_sources": [r["id"] for r in records],
                "updates": [],
            }, {r["id"]: {"path": r["archive"], "kind": "user"} for r in records}

        with patch.object(summarize, "run", side_effect=finish):
            hooks.recover(self.store, self.task, force=True)
        self.assertIn("C" * 3000, "".join(recovered))
        self.assertEqual(self.store.load(self.task)["pending"], [])

    def test_project_promotion_is_conservative_idempotent_and_preserves_edits(self):
        cp = memory.empty_checkpoint()
        state = self.store.save_agent(
            self.task,
            {
                "checkpoint": cp,
                "updates": [{"kind": "finding", "title": "Fact", "body": "Verified fact", "confidence": "confirmed"}],
            },
        )
        entry = state["entries"][0]["id"]
        path = self.store.vault / "projects" / state["identity"]["project"] / "knowledge.md"
        with path.open("a") as f:
            f.write("\nHuman annotation\n")
        self.store.promote(self.task, entry)
        self.store.promote(self.task, entry)
        text = path.read_text()
        self.assertIn("Human annotation", text)
        self.assertEqual(text.count("Verified fact"), 1)

    def test_install_twice_uninstall_preserves_foreign_hooks_and_agents(self):
        home = self.base / "codex-home"
        home.mkdir()
        other = {"type": "command", "command": "aisuite hook", "async": True, "__generator": "ya_tool_aisuite_meta"}
        original = {
            "hooks": {"SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [other]}]},
            "description": "Original",
        }
        memory.write_json(home / "hooks.json", original)
        (home / "AGENTS.md").write_text("User instructions\n")
        vault = self.base / "installed-vault"
        for _ in range(2):
            install.install(home, vault, sys.executable, sys.executable)
        doc = memory.read_json(home / "hooks.json")
        self.assertEqual(len(doc["hooks"]["PreCompact"]), 1)
        self.assertEqual(doc["hooks"]["SessionStart"][0]["hooks"][0], other)
        self.assertEqual((home / "AGENTS.md").read_text().count(install.START), 1)
        install.install(home, vault, None, sys.executable, uninstall=True)
        self.assertEqual(memory.read_json(home / "hooks.json"), original)
        self.assertEqual((home / "AGENTS.md").read_text(), "User instructions\n\n")
        self.assertTrue(vault.exists())

    def test_no_recursion_in_summarizer_role(self):
        with patch.dict(os.environ, {"PRESERVE_CONTEXT_ROLE": "summarizer"}):
            self.assertEqual(self.run_hook(), {"continue": True})
            self.assertEqual(hooks.session_start(self.store, self.payload), {"continue": True})

    def test_corrupt_archive_never_marked_complete(self):
        def fail(*args):
            raise RuntimeError("network down")

        self.run_hook(fail)
        state = self.store.load(self.task)
        (self.store.vault / state["pending"][0]["archive"]).write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "integrity"):
            hooks.recover(self.store, self.task)
        self.assertTrue(self.store.load(self.task)["pending"])

    def test_actual_child_invalid_json_retains_pending(self):
        binary = self.base / "invalid-codex"
        binary.write_text(
            "#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nsys.stdin.read()\nPath(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{invalid json')\n"
        )
        binary.chmod(0o755)
        self.store.config["codex_bin"] = str(binary)
        self.assertTrue(hooks.precompact(self.store, self.payload)["continue"])
        state = self.store.load(self.task)
        self.assertTrue(state["pending"])
        self.assertEqual(state["cursor"], {})

    def test_actual_child_timeout_killed_and_pending_retained(self):
        binary = self.base / "sleeping-codex"
        binary.write_text("#!/usr/bin/env python3\nimport sys,time\nsys.stdin.read()\ntime.sleep(20)\n")
        binary.chmod(0o755)
        self.store.config.update({"codex_bin": str(binary), "timeout_seconds": 0.1})
        self.assertTrue(hooks.precompact(self.store, self.payload)["continue"])
        state = self.store.load(self.task)
        self.assertTrue(state["pending"])
        self.assertIn("timed out", state["pending"][0]["reason"])
        self.assertEqual(state["cursor"], {})

    def test_cancelled_hook_kills_child_and_retains_pending(self):
        binary = self.base / "blocking-codex"
        pidfile = self.base / "child.pid"
        binary.write_text(
            "#!/usr/bin/env python3\nimport os,sys,time\nfrom pathlib import Path\nPath("
            + repr(str(pidfile))
            + ").write_text(str(os.getpid()))\nsys.stdin.read()\ntime.sleep(20)\n"
        )
        binary.chmod(0o755)
        memory.write_json(self.store.root / "config.json", {"codex_bin": str(binary)})
        script = Path(hooks.__file__)
        process = subprocess.Popen(
            [sys.executable, str(script), "--vault", str(self.store.vault), "precompact"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        process.stdin.write(json.dumps(self.payload).encode())
        process.stdin.close()
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pidfile.exists())
            child = int(pidfile.read_text())
            process.terminate()
            process.wait(timeout=5)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            state = self.store.load(self.task)
            self.assertTrue(state["pending"])
            self.assertEqual(state["cursor"], {})
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()

    def test_home_arc_config_does_not_merge_unrelated_projects(self):
        home = self.base / "home"
        (home / ".arc").mkdir(parents=True)
        first = home / "workspace-a"
        first.mkdir()
        second = home / "workspace-b"
        second.mkdir()
        with patch.object(Path, "home", return_value=home):
            a, root_a = self.store.project_id(first)
            b, root_b = self.store.project_id(second)
        self.assertNotEqual(a, b)
        self.assertEqual(root_a, str(first.resolve()))
        self.assertEqual(root_b, str(second.resolve()))

    def test_missing_hook_identity_does_not_use_inherited_parent_id(self):
        payload = {"session_id": "parent-root", "transcript_path": None, "cwd": str(self.base)}
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-a"}):
            first, kind = transcript.identify(payload)
            second, _ = transcript.identify(payload)
            restored = hooks.session_start(self.store, payload)
        self.assertEqual(kind, "unresolved")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, "thread-a")
        self.assertIn("identity is unavailable", restored["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.store.load(self.task)["revision"], 0)


if __name__ == "__main__":
    unittest.main()
