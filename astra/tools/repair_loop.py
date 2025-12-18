"""Closed-loop automated repair for Astra modules.

Pipeline:
1) Load module
2) Validate + run checkers + run tests
3) If issues:
   a) apply deterministic suggestions (repair_suggest)
   b) if still issues, ask an LLM provider for JSON Patch ops
   c) apply patch, re-validate, repeat

The LLM provider is pluggable (see `llm_providers.py`).

This tool is designed so you can run it locally where an LLM endpoint is available.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astra.tools import fmt, semantic, typecheck, effects, test_runner
from astra.tools.llm_providers import make_provider
from astra.tools.pointer import apply_patch
from astra.tools.repair_suggest import suggest_patches


def collect_issues(module: Dict[str, Any], *, validate_schema: bool = True, allowed_effects: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if validate_schema:
        schema = fmt.load_schema()
        errs = fmt.validate(module, schema)
        for e in errs:
            issues.append(
                {
                    "pointer": e.get("pointer", ""),
                    "code": "SchemaError",
                    "severity": "error",
                    "message": e.get("message", "schema error"),
                    "detail": {"validator": e.get("validator"), "expected": e.get("expected")},
                }
            )

    issues.extend(semantic.check_module(module))
    issues.extend(typecheck.check_module(module))
    issues.extend(effects.check_effects(module))

    if allowed_effects is None:
        allowed_effects = ["pure"]
    issues.extend(test_runner.run_tests(module, allowed_effects))

    # stable order
    issues.sort(key=lambda i: (i.get("severity", "error"), i.get("code", ""), i.get("pointer", "")))
    return issues


def build_prompt(module: Dict[str, Any], issues: List[Dict[str, Any]]) -> str:
    return (
        "You are repairing an Astra JSON-AST module.\n"
        "Return ONLY a JSON array of JSON Patch operations (RFC6902 subset: add/replace/remove).\n"
        "No prose, no markdown.\n\n"
        "Astra module JSON:\n"
        + json.dumps(module, indent=2, ensure_ascii=False)
        + "\n\nIssues (JSON):\n"
        + json.dumps(issues, indent=2, ensure_ascii=False)
        + "\n\nConstraints:\n"
        "- Preserve module semantics unless needed to fix errors\n"
        "- Prefer minimal changes\n"
        "- Keep formatting valid JSON\n"
    )


def repair_loop(
    module: Dict[str, Any],
    *,
    provider_kind: str,
    provider_cmd: Optional[str],
    max_iters: int,
    allowed_effects: List[str],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    provider = make_provider(provider_kind, cmd=provider_cmd)

    history: List[Dict[str, Any]] = []

    for it in range(max_iters):
        issues = collect_issues(module, allowed_effects=allowed_effects)
        history.append({"iter": it, "issue_count": len(issues), "issues": issues})

        errors = [i for i in issues if i.get("severity") == "error"]
        if not errors:
            break

        # 1) deterministic suggestions
        patches = suggest_patches(module, issues)
        if patches:
            module = apply_patch(module, patches)
            continue

        # 2) LLM patches
        prompt = build_prompt(module, issues)
        llm_patch = provider.propose_patches(prompt)
        if not llm_patch:
            break
        module = apply_patch(module, llm_patch)

    return module, history


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="astra repair-loop")
    ap.add_argument("path", help="Path to Astra module JSON")
    ap.add_argument("--provider", default="mock", help="mock | cmd | openai")
    ap.add_argument("--cmd", help="Command for cmd provider")
    ap.add_argument("--max-iters", type=int, default=5)
    ap.add_argument("--allowed", nargs="*", default=["pure"], help="Allowed effects for running tests")
    ap.add_argument("--out", help="Write repaired module JSON here (default: stdout)")
    ap.add_argument("--history", help="Write repair history JSON here")
    args = ap.parse_args(argv)

    try:
        module = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read/parse JSON: {e}", file=sys.stderr)
        return 3

    repaired, history = repair_loop(
        module,
        provider_kind=args.provider,
        provider_cmd=args.cmd,
        max_iters=args.max_iters,
        allowed_effects=args.allowed,
    )

    out_text = fmt.dumps_canonical(repaired)
    if args.out:
        Path(args.out).write_text(out_text, encoding="utf-8")
    else:
        sys.stdout.write(out_text)

    if args.history:
        Path(args.history).write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    final_issues = collect_issues(repaired, allowed_effects=args.allowed)
    has_errors = any(i.get("severity") == "error" for i in final_issues)
    return 2 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
