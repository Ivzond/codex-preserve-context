# Incremental checkpoints

Use the task ID and installed skill path supplied by SessionStart. Initialize
only if no task memory exists. If a checkpoint was supplied inline, use it
directly; do not reread the same file.

One save can combine nearby changes:

```sh
python3 <skill-dir>/scripts/memory_store.py save --task <id> --input - <<'JSON'
{
  "checkpoint": {
    "status": "active",
    "current_question": "Verify event 42",
    "next_actions": ["Compare activity order at event 42"]
  },
  "updates": [{
    "kind": "finding", "title": "Retry branch differs",
    "body": "Current source adds an activity; test/log path: /work/replay.log.",
    "confidence": "confirmed", "supersedes": []
  }]
}
JSON
```

Omitted checkpoint fields remain unchanged; arrays supplied replace that field.
Updates-only saves are allowed. An unchanged checkpoint with no updates is a no-op.
Use `base_revision` when coordinating multiple writers. Main saves never advance
the summarizer cursor. Evidence IDs go in `sources`; existing entry IDs go in
`supersedes`. Unknown IDs and checkpoint fields are rejected.

On a changed objective, refresh goal, status, constraints, completed, current
question and next action together; clear old task-specific fields explicitly.
Save after a useful milestone, correction or before interruption/handoff.
Do not checkpoint a trivial answer or repeat a save for every tool call.
