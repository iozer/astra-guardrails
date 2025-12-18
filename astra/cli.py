#!/usr/bin/env python3
"""Astra unified CLI.

This CLI intentionally delegates argument parsing to the individual tool modules.
That keeps each tool usable both as:
- `astra <tool> ...`
- `python -m astra.tools.<tool> ...`

Commands:
- format        Schema validate + canonical format
- resolve       Merge imports/externs into a single module
- semantic      Semantic analysis (undefined vars, unreachable, missing return, ...)
- typecheck     Generic type checker
- effectcheck   Static effect checker
- test          Run unit tests embedded in module
- prop          Property-based tests + shrinking
- pretty        Pretty printer (textual)
- codegen       Generate Python code
- run-ast       Execute via AST interpreter
- run-py        Execute via Python codegen sandbox
- repair-loop   Closed-loop repair (deterministic + optional LLM provider)
- lsp           Start LSP server (stdio)

Example:
  astra format examples/demo.json --in-place
"""

from __future__ import annotations

import sys
from typing import List, Optional

from astra.tools import (
    fmt,
    resolve,
    semantic,
    typecheck,
    effects,
    test_runner,
    propcheck,
    pretty,
    codegen_py,
    sandbox_ast,
    sandbox_exec_py,
    repair_loop,
    lsp_server,
)


def _help() -> str:
    return (
        "Astra CLI\n\n"
        "Usage:\n"
        "  astra <command> [args...]\n\n"
        "Commands:\n"
        "  format        Validate + canonicalize JSON\n"
        "  resolve       Merge imports/externs\n"
        "  semantic      Semantic checks\n"
        "  typecheck     Type checks (generics, lists, records)\n"
        "  effectcheck   Effect checks\n"
        "  test          Run embedded unit tests\n"
        "  prop          Property tests + shrinking\n"
        "  pretty        Pretty print module\n"
        "  codegen       Generate Python code\n"
        "  run-ast       Execute via AST interpreter\n"
        "  run-py        Execute via Python codegen sandbox\n"
        "  repair-loop   Closed-loop repair (optional LLM)\n"
        "  lsp           Start language server (stdio)\n"
        "  version       Show current version\n"
    )

def _print_version_and_exit() -> None:
    try:
        from importlib.metadata import version
        v = version("astra-llm-first")
    except Exception:
        v = "unknown"
    print(v)
    raise SystemExit(0)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        sys.stdout.write(_help())
        return 0

    cmd, rest = argv[0], argv[1:]
     # global flags / pseudo-commands
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V", "version"):
        _print_version_and_exit()
    if cmd == "format":
        return fmt.main(rest)
    if cmd == "resolve":
        return resolve.main(rest)
    if cmd == "semantic":
        return semantic.main(rest)
    if cmd == "typecheck":
        return typecheck.main(rest)
    if cmd in {"effectcheck", "effects"}:
        return effects.main(rest)
    if cmd == "test":
        return test_runner.main(rest)
    if cmd == "prop":
        return propcheck.main(rest)
    if cmd == "pretty":
        return pretty.main(rest)
    if cmd == "codegen":
        return codegen_py.main(rest)
    if cmd == "run-ast":
        return sandbox_ast.main(rest)
    if cmd == "run-py":
        return sandbox_exec_py.main(rest)
    if cmd == "repair-loop":
        return repair_loop.main(rest)
    if cmd == "lsp":
        return lsp_server.main()

    sys.stderr.write(f"Unknown command: {cmd}\n\n")
    sys.stderr.write(_help())
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
