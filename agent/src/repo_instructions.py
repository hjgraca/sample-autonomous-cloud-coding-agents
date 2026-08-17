"""Repository instruction discovery for provider-neutral agent runtimes."""

from __future__ import annotations

from pathlib import Path

_MAX_INSTRUCTION_BYTES = 256 * 1024


def _modern_instruction_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    agents_md = root / "AGENTS.md"
    if agents_md.is_file():
        paths.append(agents_md)

    agents_entry = root / ".agents"
    if agents_entry.is_file():
        paths.append(agents_entry)
    elif agents_entry.is_dir():
        paths.extend(sorted(p for p in agents_entry.rglob("*.md") if p.is_file()))
    return paths


def _legacy_instruction_paths(root: Path, ignored: frozenset[str]) -> list[Path]:
    paths: list[Path] = []
    if "claude_md" not in ignored:
        paths.extend(p for p in (root / "CLAUDE.md", root / ".claude" / "CLAUDE.md") if p.is_file())
    rules = root / ".claude" / "rules"
    if "rules" not in ignored and rules.is_dir():
        paths.extend(sorted(p for p in rules.rglob("*.md") if p.is_file()))
    return paths


def load_repo_instructions(
    repo_dir: str,
    *,
    ignored: frozenset[str] = frozenset(),
) -> str:
    """Load modern repo guidance, falling back to legacy Claude files.

    A repository that provides any ``AGENTS.md``/``.agents`` guidance owns the
    instruction surface; legacy files are loaded only when no modern source is
    present. This makes precedence deterministic and avoids contradictory rules.
    """
    root = Path(repo_dir).resolve()
    paths = _modern_instruction_paths(root)
    if not paths:
        paths = _legacy_instruction_paths(root, ignored)

    sections: list[str] = []
    consumed = 0
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        encoded = content.encode("utf-8")
        remaining = _MAX_INSTRUCTION_BYTES - consumed
        if remaining <= 0:
            break
        if len(encoded) > remaining:
            content = encoded[:remaining].decode("utf-8", errors="ignore")
            encoded = content.encode("utf-8")
        consumed += len(encoded)
        sections.append(f"### {path.relative_to(root).as_posix()}\n\n{content.strip()}")

    if not sections:
        return ""
    return "\n\n## Repository instructions\n\n" + "\n\n".join(sections)
