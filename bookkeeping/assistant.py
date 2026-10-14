"""Read-only questions about the books, answered by a local AI command-line tool.

Engines are the locally signed-in `claude` and `codex` CLIs, tried in the
order configured in `books.toml`. Each runs inside the workspace with tools
limited to reading files and running `books query`, `books status`, and
`books check`. No API keys are stored by this product.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

from .workspace import ENVIRONMENT_VARIABLE, Workspace


class EngineFailed(Exception):
    pass


def _event(kind: str, **payload) -> str:
    return json.dumps({"type": kind, **payload}) + "\n"


def system_prompt(workspace: Workspace) -> str:
    company = workspace.company
    entity = f" ({company.entity})" if company.entity else ""
    return (
        f"You are the bookkeeping assistant for {company.name}{entity}. "
        f"Amounts are in {company.currency} unless stated. The books are a Beancount "
        f"ledger at {workspace.relative(workspace.ledger_main)}; books.toml names the "
        "accounts with special roles. Answer by running `books query \"<BQL>\"`, "
        "`books status`, and reading files. You are READ-ONLY: never modify, create, "
        "or delete anything. If asked to change the books, explain that changes go "
        "through a write-enabled coding agent session (\"do the books\"). Be concise. "
        "Give exact figures with their currency and period, and show the query you "
        "used when it helps the reader verify the answer."
    )


def _which(name: str) -> str | None:
    found = shutil.which(name, path=_environment()["PATH"])
    return found


def _environment(workspace: Workspace | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    tool_dirs = [str(Path(sys.executable).parent), str(Path.home() / ".local" / "bin")]
    environment["PATH"] = os.pathsep.join([*tool_dirs, environment.get("PATH", "")])
    if workspace is not None:
        environment[ENVIRONMENT_VARIABLE] = str(workspace.root)
    return environment


def available_engines(workspace: Workspace) -> list[str]:
    return [name for name in workspace.assistant.engines if _which(name)]


def build_prompt(question: str, history: list[dict]) -> str:
    parts = []
    for turn in history[-6:]:
        role = "User" if turn.get("role") == "user" else "Assistant"
        parts.append(f"{role}: {str(turn.get('text', ''))[:4000]}")
    parts.append(f"User: {question}")
    return "\n\n".join(parts)


def _stream_claude(workspace: Workspace, prompt: str) -> Iterator[str]:
    binary = _which("claude")
    if not binary:
        raise EngineFailed("Claude CLI is not installed")
    process = subprocess.Popen(
        [
            binary,
            "-p",
            prompt,
            "--append-system-prompt",
            system_prompt(workspace),
            "--allowedTools",
            "Read,Grep,Glob,Bash(books query:*),Bash(books status:*),Bash(books check:*)",
            "--disallowedTools",
            "Edit,Write,NotebookEdit",
            "--max-turns",
            "20",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
        ],
        cwd=workspace.root,
        env=_environment(workspace),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    watchdog = threading.Timer(workspace.assistant.timeout_seconds, process.kill)
    watchdog.start()
    finished = False
    try:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            kind = data.get("type")
            if kind == "stream_event":
                event = data.get("event") or {}
                delta = event.get("delta") or {}
                if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    yield _event("delta", text=delta.get("text", ""))
            elif kind == "assistant":
                for block in (data.get("message") or {}).get("content", []):
                    if block.get("type") == "tool_use":
                        detail = block.get("input") or {}
                        summary = detail.get("command") or detail.get("file_path") or detail.get("pattern") or ""
                        yield _event("status", text=f"Checking: {str(summary)[:140]}")
            elif kind == "result":
                if data.get("is_error"):
                    raise EngineFailed("Claude returned an error")
                finished = True
                yield _event("final", engine="claude", text=data.get("result") or "")
        process.wait(timeout=10)
    finally:
        watchdog.cancel()
        if process.poll() is None:
            process.kill()
    if not finished:
        raise EngineFailed("Claude stopped without an answer")


def _ask_codex(workspace: Workspace, prompt: str) -> str:
    binary = _which("codex")
    if not binary:
        raise EngineFailed("Codex CLI is not installed")
    with tempfile.NamedTemporaryFile(mode="r", suffix=".txt") as last_message:
        result = subprocess.run(
            [
                binary,
                "exec",
                "--sandbox",
                "read-only",
                "--cd",
                str(workspace.root),
                "--output-last-message",
                last_message.name,
                f"{system_prompt(workspace)}\n\n{prompt}",
            ],
            env=_environment(workspace),
            capture_output=True,
            text=True,
            timeout=workspace.assistant.timeout_seconds,
        )
        answer = Path(last_message.name).read_text(encoding="utf-8").strip()
    if result.returncode != 0 or not answer:
        raise EngineFailed("Codex stopped without an answer")
    return answer


ENGINE_LABELS = {"claude": "Claude", "codex": "Codex"}


def answer(workspace: Workspace, question: str, history: list[dict]) -> Iterator[str]:
    """Yield NDJSON events: status, delta, final, or error."""
    prompt = build_prompt(question, history)
    engines = available_engines(workspace)
    if not engines:
        yield _event(
            "error",
            text="No assistant is available. Install and sign in to the Claude or Codex "
            "command-line tool, then try again.",
        )
        return
    failures = []
    for engine in engines:
        label = ENGINE_LABELS.get(engine, engine)
        yield _event("status", text=f"{label} is reading the ledger…")
        try:
            if engine == "claude":
                yield from _stream_claude(workspace, prompt)
            elif engine == "codex":
                yield _event("final", engine="codex", text=_ask_codex(workspace, prompt))
            else:
                raise EngineFailed(f"Unknown engine {engine}")
            return
        except (EngineFailed, OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{label}: {exc}")
            yield _event("status", text=f"{label} could not answer ({exc}).")
    yield _event(
        "error",
        text="No answer this time. " + "; ".join(failures) + ". Nothing in the books changed. Try again.",
    )
