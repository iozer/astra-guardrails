"""Unit test runner for Astra.

Astra tests are AST-level and live either:
- at module root: `tests: [...]`
- or inside a function: `functions[i].tests: [...]`

Each test case:
- evaluates `args` expressions
- runs the function
- evaluates `expect` expression
- compares with Python `==`

Test execution uses the AST sandbox (`sandbox_ast`) so effects are enforced.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from astra.tools import sandbox_ast


@dataclass(frozen=True)
class TestFailure:
    pointer: str
    code: str
    message: str
    detail: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pointer": self.pointer,
            "code": self.code,
            "severity": "error",
            "message": self.message,
            "detail": self.detail,
        }


def _index_fns(module: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for f in module.get("functions", []) or []:
        if isinstance(f, dict) and isinstance(f.get("name"), str):
            out[f["name"]] = f
    return out


def _eval(expr: Any, module: Dict[str, Any]) -> Any:
    # evaluate expr in empty env (no vars)
    fns = _index_fns(module)
    return sandbox_ast.eval_expr(expr, {}, fns)


def run_testcase(module: Dict[str, Any], fn_name: str, args_exprs: List[Any], expect_expr: Any, allowed_effects: List[str]) -> Tuple[bool, Any, Any, Optional[str]]:
    try:
        args = [_eval(a, module) for a in args_exprs]
        expected = _eval(expect_expr, module)
        actual = sandbox_ast.run_module(module, fn_name, args, allowed_effects)
        return (actual == expected), actual, expected, None
    except Exception as e:
        return False, None, None, str(e)


def run_tests(module: Dict[str, Any], allowed_effects: List[str]) -> List[Dict[str, Any]]:
    failures: List[TestFailure] = []

    # module-level tests
    for ti, tc in enumerate(module.get("tests", []) or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("fn")
        args = tc.get("args", []) or []
        exp = tc.get("expect")
        if not isinstance(fn, str) or not isinstance(args, list):
            continue
        ok, actual, expected, err = run_testcase(module, fn, args, exp, allowed_effects)
        if not ok:
            failures.append(
                TestFailure(
                    pointer=f"/tests/{ti}",
                    code="TestFailed" if err is None else "TestError",
                    message=f"Test {tc.get('name') or ti} failed for {fn}",
                    detail={"expected": expected, "actual": actual, "error": err},
                )
            )

    # function-level tests
    for fi, fn in enumerate(module.get("functions", []) or []):
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            continue
        name = fn["name"]
        for ti, tc in enumerate(fn.get("tests", []) or []):
            if not isinstance(tc, dict):
                continue
            args = tc.get("args", []) or []
            exp = tc.get("expect")
            if not isinstance(args, list):
                continue
            ok, actual, expected, err = run_testcase(module, name, args, exp, allowed_effects)
            if not ok:
                failures.append(
                    TestFailure(
                        pointer=f"/functions/{fi}/tests/{ti}",
                        code="TestFailed" if err is None else "TestError",
                        message=f"Function test {tc.get('name') or ti} failed for {name}",
                        detail={"expected": expected, "actual": actual, "error": err},
                    )
                )

    return [f.to_dict() for f in failures]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="astra test")
    ap.add_argument("path", help="Path to Astra module JSON")
    ap.add_argument("--allowed", nargs="*", default=["pure"], help="Allowed effects")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        module = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read/parse JSON: {e}", file=sys.stderr)
        return 3

    failures = run_tests(module, args.allowed)
    if args.json:
        print(json.dumps(failures, indent=2, ensure_ascii=False))
    else:
        for f in failures:
            print(f"{f['pointer']}: {f['message']} ({f['detail']})")

    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
