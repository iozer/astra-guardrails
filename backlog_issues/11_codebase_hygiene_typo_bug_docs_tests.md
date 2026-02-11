# Codebase hygiene sweep: typo, bug, docs consistency, and test depth

Goal: identify and prioritize small but high-leverage maintenance tasks found during repository review.

## 1) Typo fix task — clean accidental leading character in VS Code setup doc
Context:
- `VSCODE_SETUP.md` currently starts with a stray `\` before the heading.

Task:
- Remove the stray character so the file starts directly with `# VS Code setup (Astra Guardrails)`.

Acceptance criteria:
- First line of `VSCODE_SETUP.md` is a valid Markdown heading (no leading `\`).
- Markdown preview no longer shows an isolated backslash line.

## 2) Bug fix task — robust command parsing for `cmd` LLM provider
Context:
- `astra/tools/llm_providers.py` builds command arguments with `cmd.split()`, which breaks quoted arguments and escaped spaces.

Task:
- Replace naive splitting with `shlex.split(cmd)` and add clear error handling for malformed command strings.

Acceptance criteria:
- Quoted values like `--arg "hello world"` are preserved as a single argument.
- Existing simple commands continue to work.
- Invalid shell-like command strings return a helpful validation error.

## 3) Comment/documentation consistency task — align CLI command documentation
Context:
- `astra/cli.py` module docstring command list does not include `version`, but `_help()` exposes a `version` command.

Task:
- Make command lists consistent between module-level docs and runtime help text.

Acceptance criteria:
- `version` is either documented in both places or removed from both places intentionally.
- No command appears in one list but not the other.

## 4) Test improvement task — add regression tests for CLI/provider parsing edge cases
Context:
- There is no dedicated test suite directory; key behaviors rely on manual checks.

Task:
- Add focused tests for:
  - `make_provider(kind="cmd", cmd=...)` parsing behavior (quoted args, escaped spaces, malformed input).
  - CLI `--version`/`version` dispatch behavior in `astra/cli.py`.

Acceptance criteria:
- Tests fail before fix and pass after fix.
- At least one negative test validates malformed command handling.
- Tests run in CI without network access.

---
**Labels:** dx, docs, bug, tests  
**Milestone:** M2 — DevX & Tooling
