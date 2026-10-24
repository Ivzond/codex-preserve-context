"""Version-tolerant JSONL adapter. Raw data is always archived before parsing."""

import json
import os
from pathlib import Path
import uuid
from memory_store import atomic_write, digest, write_json, now

IGNORED_RECORDS = {"session_meta", "turn_context", "world_state", "token_usage_record"}
IGNORED_EVENTS = {
    "token_count",
    "task_started",
    "task_complete",
    "turn_aborted",
    "thread_settings_applied",
    "item_started",
    "item_completed",
    "context_compacted",
    "user_message",
    "agent_message",
    "agent_reasoning",
}


def identify(payload):
    path = payload.get("transcript_path")
    if path:
        try:
            with Path(path).open("rb") as f:
                # Metadata precedes content in installed Codex; no full-history scan.
                for _ in range(10):
                    line = f.readline(1024 * 1024)
                    if not line:
                        break
                    record = json.loads(line)
                    if record.get("type") == "session_meta":
                        concrete = record.get("payload", {}).get("id")
                        if isinstance(concrete, str) and concrete:
                            return concrete, "thread"
        except (OSError, ValueError, TypeError):
            pass
        if payload.get("thread_id"):
            return payload["thread_id"], "thread"
        # Never fall back to parent session_id for a fork with unknown metadata.
        return "path-" + digest(str(Path(path).resolve()))[:24], "transcript_path"
    # A hook can inherit an outer process's CODEX_THREAD_ID. Unlike a tool run
    # in the current chat, that environment value is not an identity guarantee.
    concrete = payload.get("thread_id")
    if concrete:
        return concrete, "thread"
    return "unresolved-" + uuid.uuid4().hex, "unresolved"


def content_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, dict) and block.get("type") in ("input_image", "image", "input_audio", "audio"):
                parts.append("[non-text evidence; see original transcript]")
        return "\n".join(parts)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def normalize(record, has_response_messages=True):
    """Return (kind, text, partial_coverage). Never extract private reasoning."""
    if not isinstance(record, dict):
        return "unknown", content_text(record), True
    typ, p = record.get("type"), record.get("payload", {})
    if typ in IGNORED_RECORDS:
        return None, "", False
    if typ == "response_item" and isinstance(p, dict):
        kind = p.get("type")
        if kind == "message":
            if p.get("role") in ("system", "developer"):
                return None, "", False
            if p.get("channel") == "analysis":
                return None, "", False
            text = content_text(p.get("content", []))
            return p.get("role", "message"), text, "[non-text evidence;" in text
        if kind == "reasoning":
            # Encrypted/private reasoning is deliberately outside this memory format.
            return None, "", False
        if kind in ("function_call", "custom_tool_call", "tool_call"):
            return (
                "tool_call",
                p.get("name", "tool") + "\n" + content_text(p.get("arguments", p.get("input", ""))),
                False,
            )
        if kind in ("function_call_output", "custom_tool_call_output", "tool_result"):
            return (
                "tool_output",
                "call_id=" + str(p.get("call_id", "")) + "\n" + content_text(p.get("output", "")),
                False,
            )
        if kind == "compaction":
            return (
                "previous_compaction",
                (
                    content_text(p.get("encrypted_content", "[opaque native compaction]"))
                    if not p.get("encrypted_content")
                    else "[opaque native compaction]"
                ),
                True,
            )
    if typ == "compacted":
        # Original records remain archived. A native summary can be incomplete.
        return "previous_compaction", content_text(p.get("message", "")), True
    if typ == "event_msg" and isinstance(p, dict):
        event = p.get("type")
        if event in ("user_message", "agent_message") and not has_response_messages:
            return "user" if event == "user_message" else "assistant", str(p.get("message", "")), False
        if event in IGNORED_EVENTS:
            return None, "", False
        # Parsed but unrecognized records are supplied as opaque sourced data.
    return "unknown", json.dumps(record, ensure_ascii=False, sort_keys=True), True


def archive(store, task, payload, cursor):
    """Capture a stable file snapshot; returns bytes and an immutable archive ref."""
    path = payload.get("transcript_path")
    issue = None
    try:
        if not path:
            raise FileNotFoundError("transcript_path is unavailable")
        with Path(path).open("rb") as f:
            size = os.fstat(f.fileno()).st_size
            data = f.read(size)
    except OSError as error:
        data = b""
        issue = "Transcript unavailable: " + str(error)
    offset = cursor.get("offset", 0)
    reset = False
    if (
        type(offset) is not int
        or offset < 0
        or offset > len(data)
        or (offset and digest(data[:offset]) != cursor.get("prefix_sha256"))
    ):
        offset = 0
        reset = True
        issue = issue or "Transcript replaced/truncated; reprocessing available records"
    # Save the full raw snapshot so recovery never depends on a still-existing
    # original prefix. Only the cursor-relative DELTA is sent to the model.
    raw = (
        data
        if data
        else (json.dumps({"hook_input": payload, "coverage": issue}, ensure_ascii=False) + "\n").encode("utf-8")
    )
    key = digest(raw)
    filename = str(len(data)) + "-" + key[:24] + (".jsonl" if data else ".missing.json")
    dest = store.state_dir(task) / "archives" / filename
    if dest.exists():
        if digest(dest.read_bytes()) != key:
            raise OSError("An existing raw archive is corrupted")
    else:
        atomic_write(dest, raw)
    info = {
        "archive": str(dest.relative_to(store.vault)),
        "captured": now(),
        "transcript_path": path,
        "start": offset,
        "end": len(data),
        "sha256": key,
        "issue": issue,
        "reset": reset,
    }
    if not dest.with_suffix(dest.suffix + ".meta.json").exists():
        write_json(dest.with_suffix(dest.suffix + ".meta.json"), info)
    return data, info


def batches(data, info, cursor, max_chars=60000):
    """Bounded normalized chunks, with record fragments acknowledged transactionally."""
    start = info["start"]
    fragment = 0 if info["reset"] else cursor.get("fragment", 0)
    if type(fragment) is not int or fragment < 0:
        fragment = 0
    # Tail without a newline can still be a record currently being written.
    complete_end = data.rfind(b"\n") + 1
    lines = data[start:complete_end].splitlines(keepends=True)
    has_messages = any(b'"response_item"' in line and b'"message"' in line for line in lines)
    collected, chars = [], 0
    partial = bool(info["issue"])
    last_cursor = {"offset": start, "prefix_sha256": digest(data[:start]), "fragment": fragment}
    offset = start
    part_size = min(16000, max_chars // 2)
    if part_size < 1024:
        raise ValueError("max_input_chars too small")
    for line in lines:
        end = offset + len(line)
        record_sha = digest(line)
        try:
            record = json.loads(line)
            kind, text, unfamiliar = normalize(record, has_messages)
        except (ValueError, TypeError, KeyError) as error:
            record = {}
            kind, text, unfamiliar = "malformed", line.decode("utf-8", errors="replace"), True
        partial = partial or unfamiliar
        if not kind or not text:
            last_cursor = {"offset": end, "prefix_sha256": digest(data[:end]), "fragment": 0}
            offset = end
            fragment = 0
            continue
        parts = [text[i : i + part_size] for i in range(0, len(text), part_size)]
        first = fragment if offset == start and cursor.get("record_sha256") == record_sha and not info["reset"] else 0
        for part_no in range(first, len(parts)):
            source = {
                "id": "r-" + record_sha[:20] + "-" + str(offset) + "-p" + str(part_no),
                "kind": kind,
                "text": parts[part_no],
                "timestamp": record.get("timestamp") if isinstance(record, dict) else None,
                "byte_start": offset,
                "byte_end": end,
                "part": part_no + 1,
                "parts": len(parts),
            }
            weight = len(json.dumps(source, ensure_ascii=False))
            if collected and chars + weight > max_chars:
                yield collected, last_cursor, partial
                collected, chars = [], 0
            collected.append(source)
            chars += weight
            if part_no == len(parts) - 1:
                last_cursor = {"offset": end, "prefix_sha256": digest(data[:end]), "fragment": 0}
            else:
                last_cursor = {
                    "offset": offset,
                    "prefix_sha256": digest(data[:offset]),
                    "fragment": part_no + 1,
                    "record_sha256": record_sha,
                }
        offset = end
        fragment = 0
    if complete_end < len(data):
        partial = True
    if collected or last_cursor != cursor:
        yield collected, last_cursor, partial
