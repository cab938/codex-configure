#!/usr/bin/env python3
"""Small, stdlib-only JSONL canary for the provider-model picker spike.

The caller supplies an already-built patched ``codex`` binary, a dedicated
CODEX_HOME, and that home's config.toml. The process inherits credentials from
the caller; this program never reads, prints, or persists their values.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Mapping


UMICH_MODEL = "umich-toolkit::gpt-5.6-terra"
UMICH_KEY_ENV = "UMICH_TOOLKIT_API_KEY"
OPENAI_MARKER = "CANARY_OPENAI_OK"
UMICH_MARKER = "CANARY_UM_OK"


class CanaryError(RuntimeError):
    """An actionable canary failure without echoing protocol or env secrets."""


def json_line(message: Mapping[str, Any]) -> bytes:
    """Encode one app-server JSON-RPC message as a newline-delimited frame."""
    return (json.dumps(message, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def parse_json_line(line: str | bytes) -> dict[str, Any]:
    """Parse one JSONL frame and require an object envelope."""
    try:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanaryError("app-server emitted an invalid JSONL frame") from exc
    if not isinstance(value, dict):
        raise CanaryError("app-server JSONL frame is not an object")
    return value


def _field_strings(entry: Mapping[str, Any]) -> tuple[str, ...]:
    # Preserve the catalog's order: selecting the first listed OpenAI model
    # should be reproducible when an entry exposes both id and model.
    return tuple(
        value
        for key in ("id", "model")
        if isinstance(value := entry.get(key), str)
    )


def catalog_models(result: Mapping[str, Any]) -> tuple[str, str]:
    """Validate the required qualified entries and return (OpenAI, U-M)."""
    data = result.get("data")
    if not isinstance(data, list):
        raise CanaryError("model/list result has no data array")

    openai_model: str | None = None
    saw_umich = False
    for item in data:
        if not isinstance(item, dict):
            continue
        values = _field_strings(item)
        if UMICH_MODEL in values:
            saw_umich = True
        if openai_model is None:
            openai_model = next((value for value in values if value.startswith("openai::")), None)

    if openai_model is None:
        raise CanaryError("model/list did not return a qualified OpenAI model")
    if not saw_umich:
        raise CanaryError(f"model/list did not return {UMICH_MODEL}")
    return openai_model, UMICH_MODEL


def thread_id_from_start(result: Mapping[str, Any]) -> str:
    thread = result.get("thread")
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        raise CanaryError("thread/start result has no thread id")
    return thread_id


def turn_id_from_start(result: Mapping[str, Any]) -> str:
    turn = result.get("turn")
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise CanaryError("turn/start result has no turn id")
    return turn_id


def validate_resumed_selection(result: Mapping[str, Any], *, thread_id: str) -> None:
    thread = result.get("thread")
    resumed_thread_id = thread.get("id") if isinstance(thread, dict) else None
    if resumed_thread_id != thread_id:
        raise CanaryError("thread/resume did not restore the original task id")
    if result.get("model") != UMICH_MODEL:
        raise CanaryError("thread/resume did not restore the qualified U-M model")
    if result.get("modelProvider") != "umich-toolkit":
        raise CanaryError("thread/resume did not restore the U-M provider")


def completed_marker(
    notification: Mapping[str, Any],
    *,
    thread_id: str,
    turn_id: str,
    expected: str,
) -> str:
    """Validate completion identity and return the sole final agent message."""
    params = notification.get("params")
    turn = params.get("turn") if isinstance(params, dict) else None
    got_thread = params.get("threadId") if isinstance(params, dict) else None
    got_turn = turn.get("id") if isinstance(turn, dict) else None
    if got_thread != thread_id or got_turn != turn_id:
        raise CanaryError("turn/completed did not preserve the task or turn id")
    items = turn.get("items") if isinstance(turn, dict) else None
    if not isinstance(items, list):
        raise CanaryError("turn/completed result has no item list")
    messages = [
        item.get("text")
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "agentMessage"
        and isinstance(item.get("text"), str)
    ]
    if messages != [expected]:
        raise CanaryError(f"expected exactly one agent marker for turn {turn_id}")
    return messages[0]


class AppServer:
    def __init__(self, binary: Path, codex_home: Path, config: Path, timeout: float) -> None:
        self.binary = binary.resolve(strict=True)
        self.codex_home = codex_home.resolve(strict=True)
        self.config = config.resolve(strict=True)
        self.timeout = timeout
        expected_config = (self.codex_home / "config.toml").resolve()
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise CanaryError(f"codex binary is not executable: {self.binary}")
        if not self.codex_home.is_dir():
            raise CanaryError(f"CODEX_HOME is not a directory: {self.codex_home}")
        if not self.config.is_file() or self.config != expected_config:
            raise CanaryError("config must be the supplied dedicated CODEX_HOME/config.toml")
        if UMICH_KEY_ENV not in os.environ:
            raise CanaryError(f"{UMICH_KEY_ENV} must be supplied by the parent environment")
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        self.process = subprocess.Popen(
            [str(self.binary), "app-server", "--stdio"],
            cwd=str(self.codex_home),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self._selector = selectors.DefaultSelector()
        self._selector.register(self.process.stdout, selectors.EVENT_READ)
        self._next_id = 1
        self._notifications: deque[dict[str, Any]] = deque()

    def close(self) -> None:
        self._selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        else:
            self.process.wait()

    def _read_message(self, context: str) -> dict[str, Any]:
        assert self.process.stdout is not None
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CanaryError(f"timed out waiting for {context}")
            ready = self._selector.select(remaining)
            if not ready:
                raise CanaryError(f"timed out waiting for {context}")
            line = self.process.stdout.readline()
            if not line:
                code = self.process.poll()
                raise CanaryError(f"app-server exited while waiting for {context} (status {code})")
            return parse_json_line(line)

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        request: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = dict(params)
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json_line(request))
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CanaryError(f"could not send {method} request") from exc

        while True:
            message = self._read_message(f"{method} response")
            if message.get("id") != request_id:
                self._notifications.append(message)
                continue
            error = message.get("error")
            if isinstance(error, dict):
                # Do not include error data: a server diagnostic must never
                # become an accidental credential log.
                code = error.get("code", "unknown")
                raise CanaryError(f"{method} returned JSON-RPC error code {code}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise CanaryError(f"{method} response has no object result")
            return result

    def notification(self, method: str) -> dict[str, Any]:
        for _ in range(len(self._notifications)):
            message = self._notifications.popleft()
            if message.get("method") == method and "id" not in message:
                return message
            self._notifications.append(message)
        while True:
            message = self._read_message(f"{method} notification")
            if message.get("method") == method and "id" not in message:
                return message
            self._notifications.append(message)


def run(args: argparse.Namespace) -> None:
    thread_id: str | None = None
    server = AppServer(
        args.codex,
        args.codex_home,
        args.codex_home / "config.toml",
        args.timeout,
    )
    try:
        _initialize(server)
        catalog = server.request("model/list", {"limit": 100, "includeHidden": True})
        openai_model, umich_model = catalog_models(catalog)
        print(f"catalog ok: {openai_model}, {umich_model}")
        if args.catalog_only:
            return

        thread_result = server.request(
            "thread/start",
            {
                "model": openai_model,
                "cwd": str(args.cwd.resolve()),
                "ephemeral": False,
                "approvalPolicy": "never",
                "sandbox": "read-only",
            },
        )
        thread_id = thread_id_from_start(thread_result)

        first_turn = _start_turn(server, thread_id, openai_model, OPENAI_MARKER)
        first_completed = server.notification("turn/completed")
        completed_marker(
            first_completed,
            thread_id=thread_id,
            turn_id=first_turn,
            expected=OPENAI_MARKER,
        )

        second_turn = _start_turn(server, thread_id, umich_model, UMICH_MARKER)
        second_completed = server.notification("turn/completed")
        completed_marker(
            second_completed,
            thread_id=thread_id,
            turn_id=second_turn,
            expected=UMICH_MARKER,
        )
        print(f"turns ok: task {thread_id} kept across OpenAI -> U-M")
    finally:
        server.close()

    assert thread_id is not None
    resumed_server = AppServer(
        args.codex,
        args.codex_home,
        args.codex_home / "config.toml",
        args.timeout,
    )
    try:
        _initialize(resumed_server)
        resumed = resumed_server.request(
            "thread/resume",
            {"threadId": thread_id, "excludeTurns": True},
        )
        validate_resumed_selection(resumed, thread_id=thread_id)
        print(f"resume ok: task {thread_id} restored {UMICH_MODEL}")
    finally:
        resumed_server.close()


def _initialize(server: AppServer) -> None:
    server.request(
        "initialize",
        {
            "clientInfo": {
                "name": "codex_provider_model_picker_canary",
                "title": "Provider-model picker canary",
                "version": "0.1.0",
            },
            "capabilities": {"experimentalApi": True},
        },
    )
    _send_notification(server, "initialized", {})


def _send_notification(server: AppServer, method: str, params: Mapping[str, Any]) -> None:
    assert server.process.stdin is not None
    message: dict[str, Any] = {"method": method}
    if params:
        message["params"] = dict(params)
    try:
        server.process.stdin.write(json_line(message))
        server.process.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        raise CanaryError(f"could not send {method} notification") from exc


def _start_turn(server: AppServer, thread_id: str, model: str, marker: str) -> str:
    result = server.request(
        "turn/start",
        {
            "threadId": thread_id,
            "model": model,
            "input": [
                {
                    "type": "text",
                    "text": f"Reply with exactly {marker} and no other text.",
                    "textElements": [],
                }
            ],
        },
    )
    return turn_id_from_start(result)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True, help="patched codex binary")
    parser.add_argument("--codex-home", type=Path, required=True, help="dedicated CODEX_HOME")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="isolated task working directory")
    parser.add_argument("--timeout", type=float, default=45.0, help="per-frame timeout in seconds")
    parser.add_argument("--catalog-only", action="store_true", help="stop after model/list")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if not args.cwd.is_dir():
            raise CanaryError(f"task cwd is not a directory: {args.cwd}")
        run(args)
    except (CanaryError, OSError, ValueError) as exc:
        print(f"canary failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
