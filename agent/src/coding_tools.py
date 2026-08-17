"""Provider-neutral Strands tools for autonomous repository work."""

from __future__ import annotations

import glob as glob_module
import ipaddress
import os
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from strands import tool

_MAX_TOOL_OUTPUT = 200_000
_MAX_FETCH_BYTES = 2_000_000
_MAX_FETCH_REDIRECTS = 5
_MAX_SEARCH_RESULTS = 10_000


class RepoPathError(ValueError):
    """A requested file path escapes the checked-out repository."""


def resolve_repo_path(repo_root: str, requested: str) -> Path:
    """Resolve a model-supplied path and reject repository escapes."""
    root = Path(repo_root).resolve()
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RepoPathError(f"path escapes repository: {requested!r}") from exc
    return resolved


def _truncate_output(value: str) -> str:
    if len(value) <= _MAX_TOOL_OUTPUT:
        return value
    return value[:_MAX_TOOL_OUTPUT] + "\n[output truncated]"


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must use http or https")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"url host cannot be resolved: {parsed.hostname}") from exc
    if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("url must resolve only to public IP addresses")


def build_coding_tools(
    repo_root: str,
    *,
    enabled_tools: set[str] | None = None,
    clarification_state: dict[str, str] | None = None,
) -> list[Any]:
    """Build the complete ABCA coding-tool set bound to one repository root."""
    root = Path(repo_root).resolve()
    clarification = clarification_state if clarification_state is not None else {}

    @tool(name="shell")
    def shell(command: str, timeout_seconds: int = 600) -> str:
        """Run a shell command from the repository root and return stdout/stderr."""
        timeout = min(max(timeout_seconds, 1), 3600)
        # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
        # Shell syntax is required for coding tasks; Cedar gates every call.
        completed = subprocess.run(  # noqa: S602
            command,
            cwd=root,
            shell=True,
            executable="/bin/bash",
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        output = completed.stdout
        if completed.stderr:
            output += ("\n" if output else "") + completed.stderr
        return _truncate_output(f"exit_code={completed.returncode}\n{output}".rstrip())

    @tool(name="read_file")
    def read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read a UTF-8 text file inside the repository with optional line bounds."""
        target = resolve_repo_path(str(root), path)
        lines = target.read_text(encoding="utf-8").splitlines()
        start = max(offset, 0)
        count = min(max(limit, 1), 10_000)
        return "\n".join(lines[start : start + count])

    @tool(name="write_file")
    def write_file(path: str, content: str) -> str:
        """Create or replace a UTF-8 text file inside the repository."""
        target = resolve_repo_path(str(root), path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content.encode('utf-8'))} bytes to {target.relative_to(root)}"

    @tool(name="edit_file")
    def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        """Replace exact text in a repository file; fail when the match is ambiguous."""
        target = resolve_repo_path(str(root), path)
        content = target.read_text(encoding="utf-8")
        occurrences = content.count(old_text)
        if occurrences == 0:
            raise ValueError("old_text was not found")
        if occurrences > 1 and not replace_all:
            raise ValueError(f"old_text matched {occurrences} times; set replace_all=true")
        updated = content.replace(old_text, new_text, -1 if replace_all else 1)
        target.write_text(updated, encoding="utf-8")
        replacements = occurrences if replace_all else 1
        return f"updated {target.relative_to(root)} ({replacements} replacement(s))"

    @tool(name="glob_files")
    def glob_files(pattern: str) -> str:
        """List repository files matching a glob pattern."""
        if os.path.isabs(pattern):
            raise RepoPathError("glob pattern must be repository-relative")
        matches: list[str] = []
        for raw in glob_module.iglob(str(root / pattern), recursive=True):
            path = resolve_repo_path(str(root), raw)
            if path.is_file():
                matches.append(path.relative_to(root).as_posix())
        return "\n".join(sorted(set(matches))[:10_000])

    @tool(name="search_text")
    def search_text(query: str, pattern: str = "**/*") -> str:
        """Search UTF-8 repository files for literal text and report file:line matches."""
        results: list[str] = []
        for raw in glob_module.iglob(str(root / pattern), recursive=True):
            path = resolve_repo_path(str(root), raw)
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line_number, line in enumerate(lines, 1):
                if query in line:
                    rel = path.relative_to(root).as_posix()
                    results.append(f"{rel}:{line_number}:{line}")
                    if len(results) >= _MAX_SEARCH_RESULTS:
                        return _truncate_output("\n".join(results))
        return _truncate_output("\n".join(results))

    @tool(name="fetch_url")
    def fetch_url(url: str) -> str:
        """Fetch a public HTTP(S) URL and return a bounded text response."""
        current_url = url
        response = None
        for _ in range(_MAX_FETCH_REDIRECTS + 1):
            _validate_public_url(current_url)
            response = requests.get(
                current_url,
                timeout=30,
                stream=True,
                allow_redirects=False,
            )
            if not response.is_redirect:
                break
            location = response.headers.get("location")
            response.close()
            if not location:
                raise ValueError("redirect response did not include a location")
            current_url = urljoin(current_url, location)
        else:
            raise ValueError("url exceeded redirect limit")
        if response is None:  # pragma: no cover - loop always assigns or raises
            raise RuntimeError("fetch did not produce a response")
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > _MAX_FETCH_BYTES:
                raise ValueError("response exceeds 2 MB limit")
            chunks.append(chunk)
        text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        return _truncate_output(text)

    @tool(name="request_clarification")
    def request_clarification(question: str) -> str:
        """Record one clarifying question when the task cannot be implemented without guessing."""
        clarification["question"] = question.strip() or " "
        return (
            "Clarifying question recorded. Stop without changing files or opening a pull request."
        )

    tools = {
        "shell": shell,
        "read_file": read_file,
        "write_file": write_file,
        "edit_file": edit_file,
        "glob_files": glob_files,
        "search_text": search_text,
        "fetch_url": fetch_url,
        "request_clarification": request_clarification,
    }
    selected = enabled_tools if enabled_tools is not None else set(tools)
    return [tools[name] for name in tools if name in selected]
