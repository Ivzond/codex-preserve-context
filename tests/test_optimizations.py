"""Performance changes must preserve provenance, raw data and atomic progress."""

import copy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import test_memory as fixtures
from test_memory import fake_summary
import hooks
import install
import memory_store as memory
import summarize


class OptimizationTests(unittest.TestCase):
    setUp = fixtures.MemoryTests.setUp
    tearDown = fixtures.MemoryTests.tearDown
    run_hook = fixtures.MemoryTests.run_hook

    def test_reinstall_changes_only_own_instruction_block(self):
        home = self.base / 'codex-home'
        home.mkdir()
        vault = self.base / 'installed-vault'
        original = 'Before: keep spaces  \n\n' + install.bootstrap(home / 'old-skill', vault) + '\nAfter: Arc policy\n'
        (home / 'AGENTS.md').write_text(original)
        install.install(home, vault, sys.executable, sys.executable)
        self.assertEqual(install.remove_bootstrap((home / 'AGENTS.md').read_text()), install.remove_bootstrap(original))

    def test_restore_does_not_sync_unchanged_files(self):
        self.run_hook()
        with patch.object(memory.os, 'fsync', wraps=os.fsync) as sync:
            restored = hooks.session_start(self.store, self.payload)
        self.assertEqual(sync.call_count, 0)
        context = restored['hookSpecificOutput']['additionalContext']
        self.assertIn('job-42', context)
        self.assertIn('Checkpoint (JSON data)', context)
        self.assertIn('do not reread', context)

    def test_unchanged_atomic_write_preserves_file_and_replaces_symlink(self):
        target = self.base / 'target'
        memory.atomic_write(target, 'first')
        before = target.stat()
        with patch.object(memory.os, 'fsync', wraps=os.fsync) as sync:
            memory.atomic_write(target, 'first')
        self.assertEqual(sync.call_count, 0)
        self.assertEqual(target.stat().st_ino, before.st_ino)
        alias = self.base / 'alias'
        alias.symlink_to(target)
        memory.atomic_write(alias, 'second')
        self.assertFalse(alias.is_symlink())
        self.assertEqual(alias.read_text(), 'second')
        self.assertEqual(target.read_text(), 'first')

    def test_partial_save_preserves_fields_sources_and_cursor(self):
        self.run_hook()
        before = self.store.load(self.task)
        result = self.store.save_agent(
            self.task, {'base_revision': before['revision'], 'checkpoint': {'next_actions': ['Inspect job-42']}}
        )
        expected = copy.deepcopy(before['checkpoint'])
        expected['next_actions'] = ['Inspect job-42']
        self.assertEqual(result['checkpoint'], expected)
        self.assertEqual(result['cursor'], before['cursor'])
        self.assertTrue(set(before['checkpoint_sources']) <= set(result['checkpoint_sources']))
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.store.save_agent(self.task, {'base_revision': before['revision'], 'checkpoint': {}})
        for invalid in ({'typo': 'ignored?'}, {'completed': 'wrong type'}, None):
            with self.subTest(patch=invalid), self.assertRaises(ValueError):
                self.store.save_agent(self.task, {'checkpoint': invalid})

    def test_updates_only_and_no_op_do_not_rewrite_checkpoint(self):
        self.run_hook()
        before = self.store.load(self.task)
        result = self.store.save_agent(
            self.task,
            {
                'updates': [
                    {
                        'kind': 'finding',
                        'title': 'Verified retry',
                        'body': 'Retry limit is 3',
                        'confidence': 'confirmed',
                    }
                ]
            },
        )
        self.assertEqual(result['checkpoint'], before['checkpoint'])
        self.assertEqual(result['cursor'], before['cursor'])
        revision = result['revision']
        self.assertEqual(self.store.save_agent(self.task, {'checkpoint': {}})['revision'], revision)
        for invalid in (None, {}, 'bad'):
            with self.subTest(updates=invalid), self.assertRaises(ValueError):
                self.store.save_agent(self.task, {'updates': invalid})

    def test_null_checkpoint_keeps_provenance_but_commits_new_records(self):
        self.store.save_agent(self.task, {'checkpoint': {'goal': 'Retain established goal'}})
        before = self.store.load(self.task)

        def unchanged(store, task, state, records, model=None, timeout=None):
            result, sources = fake_summary(store, task, state, records)
            result['checkpoint'] = None
            return result, sources

        self.assertTrue(self.run_hook(unchanged)['continue'])
        result = self.store.load(self.task)
        self.assertEqual(result['checkpoint'], before['checkpoint'])
        self.assertEqual(result['checkpoint_sources'], before['checkpoint_sources'])
        self.assertEqual(result['cursor']['offset'], self.path.stat().st_size)
        self.assertEqual(result['pending'], [])
        self.assertTrue(result['entries'])

    def test_known_evidence_alias_resolves_but_invented_id_is_rejected(self):
        self.run_hook()
        state = self.store.load(self.task)
        result, sources = fake_summary(
            self.store,
            self.task,
            state,
            [{'id': 'fresh', 'archive': 'fixture.jsonl', 'kind': 'user', 'text': 'Correction'}],
        )
        old_id = state['entries'][0]['id']
        result['updates'][0]['supersedes'] = ['memory-' + old_id]
        summarize.normalize_supersedes(result, state)
        self.assertEqual(result['updates'][0]['supersedes'], [old_id])
        memory.validate_result(result, state, set(sources))
        result['updates'][0]['supersedes'] = ['memory-e-invented']
        summarize.normalize_supersedes(result, state)
        with self.assertRaisesRegex(ValueError, 'superseded'):
            memory.validate_result(result, state, set(sources))
        for malformed in ({'updates': None}, {'updates': 3}, None):
            summarize.normalize_supersedes(malformed, state)

    def test_failed_validation_is_not_repeated_and_force_can_recover(self):
        def invalid(store, task, state, records, model=None, timeout=None):
            result, sources = fake_summary(store, task, state, records)
            result['checkpoint_sources'] = ['invented']
            return result, sources

        self.run_hook(invalid)
        before = self.store.load(self.task)
        with patch.object(summarize, 'run', side_effect=AssertionError('repeated model call')):
            result = hooks.recover(self.store, self.task)
            repeated = hooks.precompact(self.store, self.payload)
        self.assertFalse(result['continue'])
        self.assertTrue(repeated['continue'])
        state = self.store.load(self.task)
        self.assertEqual(state['revision'], before['revision'])
        self.assertEqual(state['cursor'], before['cursor'])
        self.assertEqual((self.store.vault / state['pending'][0]['archive']).read_bytes(), self.path.read_bytes())
        with patch.object(summarize, 'run', side_effect=fake_summary):
            self.assertTrue(hooks.recover(self.store, self.task, force=True)['continue'])

    def test_transient_retry_cooldown_then_expiry(self):
        self.run_hook(lambda *args: (_ for _ in ()).throw(TimeoutError('network timeout')))
        state = self.store.load(self.task)
        with patch.object(summarize, 'run', side_effect=AssertionError('cooldown')):
            self.assertFalse(hooks.recover(self.store, self.task)['continue'])
        after = state['pending'][0]['retry']['after']
        with (
            patch.object(hooks.time, 'time', return_value=after + 1),
            patch.object(summarize, 'run', side_effect=fake_summary),
        ):
            self.assertTrue(hooks.recover(self.store, self.task)['continue'])

    def test_config_change_permits_retry_without_force(self):
        self.run_hook(lambda *args: (_ for _ in ()).throw(ValueError('invalid output')))
        self.store.config['model'] = 'changed-model'
        with patch.object(summarize, 'run', side_effect=fake_summary):
            self.assertTrue(hooks.recover(self.store, self.task)['continue'])

    def test_repeated_compact_recreates_raw_backup_without_state_or_model_work(self):
        self.run_hook()
        before = self.store.load(self.task)
        archive = self.store.vault / before['cursor']['archive']
        archive.unlink()
        with patch.object(summarize, 'run', side_effect=AssertionError('fully covered archive')):
            self.assertTrue(hooks.precompact(self.store, self.payload)['continue'])
            self.assertTrue(hooks.recover(self.store, self.task)['continue'])
        self.assertEqual(self.store.load(self.task)['revision'], before['revision'])
        self.assertEqual(archive.read_bytes(), self.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
