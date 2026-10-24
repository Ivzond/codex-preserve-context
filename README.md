Preserve Context saves task knowledge and checkpoints across Codex context
compaction and resumes. Goals, constraints, sourced findings, decisions and the
next action live in an Obsidian-compatible Markdown vault.

The skill saves progress during work. Before compaction, `PreCompact` backs up
the transcript and runs an isolated summarizer using the existing Codex login
and model quota. `SessionStart` supplies the checkpoint to the resumed chat.
Failed summaries leave raw records pending for recovery.

Use Python 3.9+ on macOS or Linux and an authenticated local Codex CLI with
command hooks. Lifecycle integration was tested with Codex CLI
`0.159.0-alpha.12.1`.

Install from this repository:

```sh
git clone https://github.com/Ivzond/codex-preserve-context.git
cd codex-preserve-context
python3 scripts/install.py --vault "$HOME/Documents/agent-memory"
```

By default, the installer copies the skill into `~/.codex/skills/preserve-context`
and backs up and merges its hooks and marked instructions into
`~/.codex/hooks.json` and `~/.codex/AGENTS.md`. It preserves other hooks and rules.
Review and trust `hooks.py precompact` and `hooks.py session-start` through
`/hooks` in Codex, then reopen or resume the chat.

Open the configured vault in Obsidian, or read its Markdown files directly.
Save important findings as you work; source validation cannot detect every
incorrect conclusion from a summarizer.

At the next `SessionStart`, the hook queries Codex's archived-chat list and deletes
those chats' memory from the external vault. The original archived conversation
remains in Codex. Keep the vault outside this repository: notes and raw backups
can contain private transcript data.

[Checkpoint examples](references/checkpoints.md) and
[memory format, retention and configuration](references/memory-format.md)
describe the storage and recovery commands.

Run the unit tests without model calls:

```sh
python3 -m unittest discover -s tests -v
```

Remove the installed skill and its hooks while keeping the vault:

```sh
python3 scripts/install.py --vault "$HOME/Documents/agent-memory" --uninstall
```
