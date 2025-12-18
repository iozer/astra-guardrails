"""AST interpreter sandbox for Astra.

This interpreter executes the JSON-AST directly.
It enforces effects by delegating builtins to `runtime_guarded`,
which checks an allowlist set by the host.

Use cases:
- Deterministic execution for property tests
- Safe-ish execution when you do not want to execute generated Python code

Limitations:
- No loops (by design)
- Recursion is supported but can blow the Python stack if abused
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from astra.tools import runtime_guarded as rt


class ReturnSignal(Exception):
    def __init__(self, value: Any):
        super().__init__("return")
        self.value = value


class SandboxError(RuntimeError):
    pass


def _qual_last(name: str) -> str:
    return name.split(".")[-1]


def eval_expr(expr: Any, env: Dict[str, Any], fns: Dict[str, Dict[str, Any]]) -> Any:
    if isinstance(expr, (int, float, str, bool)) or expr is None:
        return expr
    if not isinstance(expr, dict):
        raise SandboxError(f"Invalid expr node: {expr!r}")

    if "var" in expr:
        name = expr["var"]
        if name not in env:
            raise SandboxError(f"Undefined variable: {name}")
        return env[name]

    if "list" in expr:
        return [eval_expr(x, env, fns) for x in expr["list"]]

    if "obj" in expr:
        return {k: eval_expr(v, env, fns) for k, v in expr["obj"].items()}

    if "call" in expr:
        call = expr["call"]
        fn = _qual_last(call["fn"])
        args = [eval_expr(a, env, fns) for a in call.get("args", [])]
        # builtin
        if fn in rt.BUILTINS:
            return rt.call_builtin(fn, args)
        # user fn
        if fn not in fns:
            raise SandboxError(f"Unknown function: {fn}")
        return call_user(fn, args, fns)

    raise SandboxError(f"Unknown expr form: {list(expr.keys())}")


def exec_stmt(stmt: Any, env: Dict[str, Any], fns: Dict[str, Dict[str, Any]]) -> None:
    if not isinstance(stmt, dict) or len(stmt.keys()) != 1:
        raise SandboxError(f"Invalid stmt shape: {stmt!r}")

    tag = next(iter(stmt.keys()))

    if tag == "let":
        spec = stmt["let"]
        name = spec["name"]
        env[name] = eval_expr(spec.get("expr"), env, fns)
        return

    if tag == "expr":
        eval_expr(stmt["expr"], env, fns)
        return

    if tag == "assert":
        spec = stmt["assert"]
        ok = eval_expr(spec.get("expr"), env, fns)
        if not ok:
            msg = spec.get("message")
            raise AssertionError(msg or "assert failed")
        return

    if tag == "return":
        raise ReturnSignal(eval_expr(stmt["return"], env, fns))

    if tag == "if":
        spec = stmt["if"]
        cond = eval_expr(spec.get("cond"), env, fns)
        block = spec.get("then", []) if cond else spec.get("else", [])
        for s in block:
            exec_stmt(s, env, fns)
        return

    raise SandboxError(f"Unknown stmt: {tag}")


def exec_block(stmts: List[Any], env: Dict[str, Any], fns: Dict[str, Dict[str, Any]]) -> Any:
    for s in stmts:
        exec_stmt(s, env, fns)
    return None


def call_user(fn_name: str, args: List[Any], fns: Dict[str, Dict[str, Any]]) -> Any:
    fn = fns[fn_name]
    params = fn.get("params", []) or []
    if len(params) != len(args):
        raise SandboxError(f"Arity mismatch calling {fn_name}: expected {len(params)} got {len(args)}")

    env: Dict[str, Any] = {p: a for p, a in zip(params, args)}
    try:
        exec_block(fn.get("body", []) or [], env, fns)
    except ReturnSignal as r:
        return r.value
    # if fell through, return None
    return None


def run_module(module: Dict[str, Any], fn: str, args: List[Any], allowed_effects: List[str]) -> Any:
    rt.set_allowed_effects(allowed_effects)

    # index functions by name (unqualified)
    fns: Dict[str, Dict[str, Any]] = {}
    for f in module.get("functions", []) or []:
        if isinstance(f, dict) and isinstance(f.get("name"), str):
            fns[f["name"]] = f

    # Install a dispatcher so higher-order builtins (list_map/list_filter/list_reduce)
    # can call user-defined functions by name.
    def _dispatch(fn_name: str, dargs: List[Any]) -> Any:
        name = _qual_last(fn_name)
        if name in rt.BUILTINS:
            return rt.call_builtin(name, dargs)
        if name not in fns:
            raise SandboxError(f"Unknown function: {fn_name}")
        return call_user(name, dargs, fns)

    rt.set_dispatch(_dispatch)

    fn_last = _qual_last(fn)
    if fn_last not in fns and fn_last not in rt.BUILTINS:
        raise SandboxError(f"Unknown function: {fn}")

    if fn_last in rt.BUILTINS:
        return rt.call_builtin(fn_last, args)

    return call_user(fn_last, args, fns)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="astra run-ast")
    ap.add_argument("path", help="Path to Astra module JSON")
    ap.add_argument("--fn", required=True, help="Function name")
    ap.add_argument("--args", nargs="*", default=[], help="Positional args (JSON literals)")
    ap.add_argument("--allowed", nargs="*", default=["pure"], help="Allowed effects")
    args = ap.parse_args(argv)

    try:
        module = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read/parse JSON: {e}", file=sys.stderr)
        return 3

    parsed_args: List[Any] = []
    for a in args.args:
        try:
            parsed_args.append(json.loads(a))
        except Exception:
            # fallback: treat as string
            parsed_args.append(a)

    try:
        out = run_module(module, args.fn, parsed_args, args.allowed)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
