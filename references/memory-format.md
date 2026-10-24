# Memory format and operations

Python 3.9+ and an authenticated local Codex CLI are required. No Obsidian plugin,
API key, vector database, service or scheduler is required. Open the configured
vault folder as an Obsidian vault. The `.state` directory stays hidden there.

## Layout

```text
projects/<project-id>/knowledge.md      # verified reusable notes; human editable
tasks/<concrete-thread-id>/resume.md    # concise current checkpoint
tasks/<id>/findings.md                 # incremental findings, confidence, sources
tasks/<id>/decisions.md                # choices, rationale, rejected approaches
tasks/<id>/journal.md                  # human notes; never overwritten
tasks/<id>/evidence/index.md           # links to raw archives and note evidence
.state/config.json                    # runtime settings
.state/registry.json                   # concrete identities, paths, projects
.state/<id>/state.json                 # atomic source of truth INCLUDING cursor
.state/<id>/cursor.json                # diagnostic mirror, never the authority
.state/<id>/archives/                  # immutable raw transcript snapshots
.state/<id>/snapshots/                 # prior state and Markdown versions
.state/<id>/staging/                   # child output and diagnostic logs
.state/install-backups/                # global files before installation
```

Generated Markdown uses YAML frontmatter and relative links, including Obsidian
block IDs for entries. It can be read without Obsidian. `state.json` makes a
multi-file checkpoint recoverable: one atomic rename commits notes and cursor
together; Markdown is regenerated from it on restore after a partial write.
Use the CLI for generated note edits. Journal and project knowledge are editable
in Obsidian; unrelated human project notes are retained. Archiving a chat opts
its task notes (including its journal) into the cleanup policy below.
Source validation catches invented IDs and
stale revisions, not semantic hallucinations. Main-agent notes remain essential.

## Save a checkpoint

The global bootstrap and SessionStart hook supply the installed skill path and
task ID. Replace `<skill-dir>` and `<task-id>` below with those actual values.

Initialize using the concrete current thread ID (normally `CODEX_THREAD_ID`):

```sh
python3 <skill-dir>/scripts/memory_store.py init --cwd /absolute/workspace
```

Write a JSON input in your workspace, then save it using:

```sh
python3 <skill-dir>/scripts/memory_store.py save --task <task-id> --input /absolute/workspace/checkpoint.json
```

Checkpoint fields are optional in a main-agent save: omitted fields retain their
values and supplied lists replace the corresponding list. See the shorter
[incremental example](checkpoints.md). An updates-only save is valid; an unchanged
checkpoint without updates creates no new revision. Clear obsolete fields when
the goal changes. A full checkpoint remains supported:


```json
{
  "checkpoint": {
    "goal": "Fix the replay failure in workflow X",
    "status": "active",
    "constraints": ["User authorized a local fix; preserve unrelated changes"],
    "completed": ["Reproduced test Y: failure at event 42; log at /work/log.txt"],
    "current_question": "Does the retry branch add an activity before event 42?",
    "working_hypotheses": ["Hypothesis: branch condition changed; not yet verified"],
    "next_actions": ["Compare activity sequence in /work/history.json at event 42"],
    "open_questions": ["Whether the old version used a different default"],
    "running_operations": [],
    "important_refs": ["Execution ID: example-123; event 42"],
    "user_corrections": []
  },
  "updates": [
    {
      "kind": "decision",
      "title": "Rejected blind history regeneration",
      "body": "It would hide the original failure. Compare commands against the existing fixture first.",
      "confidence": "confirmed",
      "supersedes": []
    }
  ]
}
```

`base_revision` is optional for a main-agent save, but recommended when multiple
writers are possible. A mismatch refuses to overwrite newer notes. An update may
refer to existing evidence in `sources`; the saved input itself is always retained
and added as a source. The main-agent save **never advances** the summary cursor.
For a correction, put old entry IDs in `supersedes` (IDs appear in generated notes).
Entries of kind `journal` are displayed in findings; the human journal is separate.

Retrieve/status/recover/promote:

```sh
python3 <skill-dir>/scripts/memory_store.py status --task <task-id>
python3 <skill-dir>/scripts/hooks.py recover --task <task-id>
python3 <skill-dir>/scripts/memory_store.py promote --task <task-id> --entry <entry-id>
```

Recovery uses immutable captured archives and can continue if the source transcript
has changed or disappeared. If auth/network is still unavailable, inspect the
pending archives through `evidence/index.md`; record only verified recovered facts.
Do not delete a pending marker or advance the cursor by hand.

An unchanged failed archive is deferred: transient failures have a five-minute
cooldown; invalid output is retried after new raw data, changed code/configuration,
or an explicit `recover --task <task-id> --force`. Integrity checks still run before
the retry guard. Use one recovery attempt when pending data affects the next
action, then inspect the evidence instead of starting repeated model calls.

The summarizer may return `checkpoint: null` to keep the checkpoint and its source
references unchanged while adding validated entries and committing the transcript
cursor. `sources` and `checkpoint_sources` contain evidence IDs; `supersedes`
contains existing entry IDs (`e-...`). An exact `memory-e-...` evidence alias can be
resolved only to an existing entry; invented IDs are still rejected.

## Installation and removal

Run from the delivered bundle (global installation can require OS/sandbox approval):

```sh
python3 /absolute/bundle/preserve-context/scripts/install.py --vault /absolute/Obsidian-vault
```

The installer backs up and merges `~/.codex/hooks.json` and the marked block in
`~/.codex/AGENTS.md`, preserving other hooks including AISuite. It is idempotent.
It does not edit `model_auto_compact_token_limit` or hook trust. Open `/hooks` in
Codex, inspect the two commands pointing to `preserve-context/scripts/hooks.py`,
and trust their exact definitions. New chats load the bootstrap; reopen/resume an
existing chat if it has cached hook configuration. Remote/cloud orchestration
may not run local command hooks; this installation targets local Codex chats.

To remove only this installation, keeping the vault and other hooks:

```sh
python3 /absolute/bundle/preserve-context/scripts/install.py --vault /absolute/Obsidian-vault --uninstall
```

Runtime knobs in `.state/config.json`: `codex_bin`, `model` (fallback; normally
use parent hook's model), `timeout_seconds` (120), `total_budget_seconds` (180),
`max_input_chars` (60000). The PreCompact handler timeout is 240 seconds; its
bounded child runs leave time to save pending status. Changing thresholds is not
part of this solution. The child uses the existing Codex login and consumes normal
model quota. Raw histories/version snapshots are retained while the chat remains
active; confirmed chat archiving opts them into SessionStart cleanup.

## Cleanup after chat archiving

Every SessionStart (`startup`, `resume`, `clear`, `compact`) queries the local
Codex app-server's `thread/list` with `archived: true`, pagination and all thread
source kinds. It compares the result with this vault's concrete thread registry.
An absent thread, shared parent session ID or unresolved identity is not evidence
of archiving. The current concrete thread is protected.

For confirmed archived chats it removes `tasks/<id>/`, the task's state, raw
records (including pending data), version snapshots, staging logs and registry
entry. The empty `task.lock` file remains so concurrent writers still use the
same lock inode. Generated `promote` blocks owned by that chat are removed from
project knowledge; unrelated project prose and other chats remain. Legacy
promotion blocks are recognized by their exact Source footer; ambiguously edited
blocks defer cleanup instead of deleting neighbouring prose. Arbitrary human
project notes without ownership markers cannot be attributed automatically.

The original archived conversation in Codex is not deleted. After successful
cleanup, unarchiving and resuming the chat creates fresh external memory. Cleanup runs at
the next SessionStart, not immediately on clicking Archive. There is no TTL and
no SessionEnd cleanup hook. API failures/malformed responses/lock contention defer
cleanup and still restore the current checkpoint; no model is called for cleanup.
The metadata lookup has a six-second default budget, followed by a bounded removal
pass. A busy or partially failed removal is retried on a later SessionStart.

Installed runtime options in `.state/config.json`:
`archive_cleanup_enabled` (true on installation),
`archive_cleanup_timeout_seconds` (6, maximum 10).

Preview the same archive check without deleting notes:

```sh
python3 <skill-dir>/scripts/archive_cleanup.py --dry-run
```

Supply `--vault /absolute/vault` when running from an uninstalled bundle.
`SessionStart` is a session-lifecycle event, not a hook for every user message or
every selection of an already loaded chat. Its sources describe startup, resume,
context clear and compaction restoration before the next model request.

Contracts verified against Codex CLI 0.159.0-alpha.12.1:
[hooks](https://learn.chatgpt.com/docs/hooks),
[non-interactive mode](https://learn.chatgpt.com/docs/non-interactive).

For a manual compact, SessionStart restoration is delivered before the next model
request. Automatic compaction inside a turn restores before its immediate
continuation. Both paths were tested against the installed native app-server.

Deterministic tests do not use a model:

```sh
python3 -m unittest discover -s <skill-dir>/tests -v
```

The optional `tests/native_lifecycle.py` performs two real model-backed lifecycle
tests using the installed hooks and current login. It creates synthetic read-only
threads and archives them at the end. Its lower automatic-compaction threshold is
an override only in its disposable test thread, never in the user's configuration.

```sh
python3 <skill-dir>/tests/native_lifecycle.py --work-dir /absolute/workspace/work/memory-check
```

The separate native archive-cleanup integration uses one brief model turn and
synthetic threads to verify the installed, trusted SessionStart callback. It checks
that archived external memory is removed while the active checkpoint and original
Codex conversation survive, then archives its remaining synthetic thread:

```sh
python3 <skill-dir>/tests/native_archive_cleanup.py --vault /absolute/vault --work-dir /absolute/workspace/work/archive-check
```
