"""Archive cleanup behavior, local IPC faults, and checkpoint preservation."""

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
import archive_cleanup as cleanup
import hooks
import memory_store as memory


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = memory.Store(self.base / "vault")
        self.store.config = {"archive_cleanup_enabled": True}
        self.current = self.store.register("current", self.base, identity_kind="thread")
        self.archived = self.store.register("archived", self.base, identity_kind="thread")
        self.active = self.store.register("active", self.base, identity_kind="thread")
        cp = memory.empty_checkpoint()
        cp["goal"] = "Current exact operation job-42"
        self.store.save_agent(self.current, {"checkpoint": cp, "updates": []})

    def tearDown(self):
        self.tmp.cleanup()

    def archived_lookup(self, store, targets, timeout):
        return {self.archived} & targets

    def state(self, task):
        return self.store.load(task)

    def test_session_start_deletes_archived_notes_raw_pending_and_versions(self):
        old = self.state(self.current)
        archived = self.state(self.archived)
        raw = self.store.state_dir(self.archived) / "archives/raw.jsonl"
        memory.atomic_write(raw, "unfinished exact transcript")
        archived["pending"] = [{"archive": str(raw.relative_to(self.store.vault))}]
        self.store.snapshot(self.archived, archived)
        self.store.commit(self.archived, archived)
        memory.atomic_write(self.store.state_dir(self.archived) / "staging/model.log", "diagnostic")
        result = hooks.session_start(
            self.store,
            {"thread_id": self.current, "cwd": str(self.base), "source": "resume"},
            archive_lookup=self.archived_lookup,
        )
        self.assertTrue(result["continue"])
        self.assertIn("job-42", result["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(self.store.task_dir(self.archived).exists())
        self.assertEqual([p.name for p in self.store.state_dir(self.archived).iterdir()], ["task.lock"])
        self.assertEqual(self.state(self.current), old)
        self.assertTrue(self.store.task_dir(self.active).exists())
        registry = memory.read_json(self.store.root / "registry.json")["tasks"]
        self.assertNotIn(self.archived, registry)
        self.assertIn(self.current, registry)

    def test_all_session_start_sources_and_unknown_identity_perform_cleanup(self):
        for i, source in enumerate(("startup", "resume", "clear", "compact")):
            task = "archived-" + str(i)
            self.store.register(task, self.base)

            def lookup(store, targets, timeout):
                return {task} & targets

            result = hooks.session_start(
                self.store, {"thread_id": self.current, "cwd": str(self.base), "source": source}, lookup
            )
            self.assertTrue(result["continue"])
            self.assertFalse(self.store.task_dir(task).exists())
        result = hooks.session_start(self.store, {"session_id": "parent"}, self.archived_lookup)
        self.assertTrue(result["continue"])
        self.assertFalse(self.store.task_dir(self.archived).exists())
        self.assertIn("identity is unavailable", result["hookSpecificOutput"]["additionalContext"])

    def test_current_unknown_and_parent_identities_are_never_cleanup_targets(self):
        unresolved = self.store.register("unresolved-one", self.base, identity_kind="unresolved")
        captured = set()

        def lookup(store, targets, timeout):
            captured.update(targets)
            return set(targets)

        cleanup.cleanup(self.store, current=self.current, lookup=lookup)
        self.assertNotIn(self.current, captured)
        self.assertNotIn(unresolved, captured)
        self.assertTrue(self.store.task_dir(self.current).exists())
        self.assertTrue(self.store.task_dir(unresolved).exists())
        # Absence from a successful archive list does not mean deletion.
        cleanup.cleanup(self.store, lookup=lambda *a: set())
        self.assertTrue(self.store.task_dir(self.current).exists())

    def test_lookup_failure_leaves_all_data_and_restores_current_checkpoint(self):
        before = self.state(self.archived)
        for error in (TimeoutError("slow lookup"), ValueError("invalid page"), OSError("no CLI")):

            def lookup(*args):
                raise error

            result = hooks.session_start(self.store, {"thread_id": self.current, "cwd": str(self.base)}, lookup)
            self.assertTrue(result["continue"])
            self.assertIn("deferred", result["systemMessage"])
            self.assertEqual(self.state(self.archived), before)
            self.assertIn("job-42", result["hookSpecificOutput"]["additionalContext"])

    def test_disabled_summarizer_and_dry_run_do_not_delete(self):
        with patch.dict(os.environ, {"PRESERVE_CONTEXT_ROLE": "summarizer"}):
            with patch.object(cleanup, "cleanup", side_effect=AssertionError("must skip")):
                self.assertEqual(hooks.session_start(self.store, {}), {"continue": True})
        result = cleanup.cleanup(self.store, lookup=self.archived_lookup, dry_run=True)
        self.assertEqual(result["candidates"], [self.archived])
        self.assertTrue(self.store.task_dir(self.archived).exists())
        self.store.config["archive_cleanup_enabled"] = False
        with patch.object(cleanup, "archived_threads", side_effect=AssertionError("disabled")):
            self.assertEqual(cleanup.cleanup(self.store)["status"], "disabled")

    def promote(self, task, title):
        state = self.state(task)
        self.store.save_agent(
            task,
            {
                "checkpoint": state["checkpoint"],
                "updates": [
                    {
                        "kind": "finding",
                        "title": title,
                        "body": "Exact confirmed body " + title,
                        "confidence": "confirmed",
                        "supersedes": [],
                    }
                ],
            },
        )
        entry = self.state(task)["entries"][0]["id"]
        self.store.promote(task, entry)
        return entry

    def test_own_promotions_removed_other_project_and_human_notes_remain(self):
        entry = self.promote(self.archived, "Archived fact")
        self.promote(self.active, "Active fact")
        project = self.state(self.archived)["identity"]["project"]
        path = self.store.vault / "projects" / project / "knowledge.md"
        # Exercise legacy promotion format alongside current bounded blocks.
        text = path.read_text().replace("<!-- /promoted:" + self.archived + ":" + entry + " -->\n", "")
        path.write_text(text + "\n## Human note\nKeep this manually edited text.\n")
        result = cleanup.cleanup(self.store, current=self.current, lookup=self.archived_lookup)
        self.assertEqual(result["removed"], [self.archived])
        kept = path.read_text()
        self.assertNotIn("Archived fact", kept)
        self.assertNotIn("../../tasks/archived/", kept)
        self.assertIn("Active fact", kept)
        self.assertIn("Human note", kept)

    def test_unbounded_edited_promotion_defers_instead_of_removing_other_text(self):
        self.promote(self.archived, "Archived fact")
        project = self.state(self.archived)["identity"]["project"]
        path = self.store.vault / "projects" / project / "knowledge.md"
        text = path.read_text()
        text = "\n".join(line for line in text.splitlines() if not line.startswith(("Source:", "<!-- /promoted:")))
        path.write_text(text)
        result = cleanup.cleanup(self.store, lookup=self.archived_lookup)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(path.read_text(), text)
        self.assertTrue(self.store.task_dir(self.archived).exists())

    def test_busy_writer_deferred_then_cleanup_is_idempotent_and_resumable(self):
        with self.store.transaction(self.archived):
            first = cleanup.cleanup(self.store, lookup=self.archived_lookup)
        self.assertEqual(first["removed"], [])
        self.assertTrue(self.store.task_dir(self.archived).exists())
        self.assertEqual(cleanup.cleanup(self.store, lookup=self.archived_lookup)["removed"], [self.archived])
        self.assertEqual(cleanup.cleanup(self.store, lookup=self.archived_lookup)["removed"], [])
        # Resuming an unarchived chat starts with new external memory.
        self.store.register(self.archived, self.base)
        self.assertEqual(self.state(self.archived)["checkpoint"]["goal"], "")

    def test_unexpected_id_symlink_and_deletion_error_preserve_unrelated_data(self):
        result = cleanup.cleanup(self.store, lookup=lambda *a: {"../outside"})
        self.assertEqual(result["removed"], [])
        with patch.object(cleanup, "remove_path", side_effect=OSError("permission denied")):
            result = cleanup.cleanup(self.store, lookup=self.archived_lookup)
        self.assertIn(self.archived, memory.read_json(self.store.root / "registry.json")["tasks"])
        self.assertTrue(self.store.task_dir(self.active).exists())
        linked = self.store.vault / "tasks"
        real = self.store.vault / "real-tasks"
        linked.rename(real)
        linked.symlink_to(real, target_is_directory=True)
        result = cleanup.cleanup(self.store, lookup=self.archived_lookup)
        self.assertEqual(result["removed"], [])
        self.assertTrue((real / self.active).exists())

    def test_removal_budget_defers_and_keeps_registry_for_a_retry(self):
        with patch.object(cleanup, "remove_path", side_effect=TimeoutError("slow directory")):
            result = hooks.session_start(
                self.store, {"thread_id": self.current, "cwd": str(self.base)}, self.archived_lookup
            )
        self.assertTrue(result["continue"])
        self.assertIn("job-42", result["hookSpecificOutput"]["additionalContext"])
        self.assertIn(self.archived, memory.read_json(self.store.root / "registry.json")["tasks"])
        with self.assertRaises(TimeoutError):
            cleanup.remove_path(self.store.task_dir(self.archived), time.monotonic() - 1)
        self.assertTrue(self.store.task_dir(self.archived).exists())
        self.assertEqual(cleanup.cleanup(self.store, lookup=self.archived_lookup)["removed"], [self.archived])


class LocalApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def binary(self, behavior):
        path = self.base / "fake-codex"
        path.write_text("#!" + sys.executable + "\nimport json, sys, time\n" + behavior)
        path.chmod(0o755)
        return str(path)

    def test_real_ipc_handshake_archive_filter_source_kinds_and_pagination(self):
        binary = self.binary('''
for line in sys.stdin:
    m=json.loads(line)
    if m['method']=='initialized': continue
    if m['method']=='initialize': result={}
    else:
        p=m['params']
        assert p['archived'] is True and p['useStateDbOnly'] is True
        assert 'appServer' in p['sourceKinds'] and 'subAgent' in p['sourceKinds']
        result={'data':[{'id':'old-a'}], 'nextCursor':'page2'} if 'cursor' not in p else {'data':[{'id':'old-b'}], 'nextCursor':None}
    print(json.dumps({'id':m['id'],'result':result}), flush=True)
''')
        client = cleanup.ArchiveClient(binary, 2)
        try:
            self.assertEqual(client.archived({'old-a', 'old-b', 'not-archived'}), {'old-a', 'old-b'})
        finally:
            client.close()
        self.assertIsNotNone(client.proc.poll())

    def test_real_ipc_timeout_invalid_page_and_error_are_not_deletion_evidence(self):
        scenarios = [
            ("time.sleep(10)\n", TimeoutError),
            (
                "for line in sys.stdin:\n m=json.loads(line)\n if 'id' in m: print(json.dumps({'id':m['id'],'result':{}}),flush=True)\n",
                ValueError,
            ),
            (
                "for line in sys.stdin:\n m=json.loads(line)\n if 'id' in m: print(json.dumps({'id':m['id'],'error':{'message':'unavailable'}}),flush=True)\n",
                RuntimeError,
            ),
        ]
        for behavior, exception in scenarios:
            client = cleanup.ArchiveClient(self.binary(behavior), 0.2)
            try:
                with self.assertRaises(exception):
                    client.archived({'old-a'})
            finally:
                client.close()
            self.assertIsNotNone(client.proc.poll())

    def test_repeated_pagination_cursor_rejects_even_previously_found_ids(self):
        binary = self.binary('''
for line in sys.stdin:
    m=json.loads(line)
    if 'id' not in m: continue
    result={} if m['method']=='initialize' else {'data':[{'id':'old-a'}],'nextCursor':'same'}
    print(json.dumps({'id':m['id'],'result':result}),flush=True)
''')
        client = cleanup.ArchiveClient(binary, 2)
        try:
            with self.assertRaises(ValueError):
                client.archived({'old-a', 'unseen'})
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
