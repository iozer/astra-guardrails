"""Semantic checker for Astra (v1.0).

Checks:
- Undefined variables (definitely-defined)
- Missing return (function may fall through)
- Unreachable statements (after guaranteed return)
- Immutable let (no rebind)
- Reserved names ("result" reserved for postconditions)
- Unknown function calls + arity mismatch (lightweight, name+arity only)

This checker intentionally does NOT do deep type inference. Use `astra typecheck` for that.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from astra.tools.pointer import join_pointer


def _qual_last(name: str) -> str:
    return name.rsplit(".", 1)[-1]

# Builtin arities (must match runtime + typechecker)
BUILTIN_ARITY: Dict[str, int] = {
    # arithmetic
    "add": 2,
    "sub": 2,
    "mul": 2,
    "div": 2,
    # comparisons
    "eq": 2,
    "neq": 2,
    "lt": 2,
    "lte": 2,
    "gt": 2,
    "gte": 2,
    # boolean
    "and": 2,
    "or": 2,
    "not": 1,
    # strings
    "str_len": 1,
    "str_concat": 2,
    "str_contains": 2,
    # lists
    "len": 1,
    "list_get": 2,
    "list_set": 3,
    "list_append": 2,
    "list_concat": 2,
    "list_slice": 3,
    "list_range": 1,
    "list_sum": 1,
    "list_mean": 1,
    "list_map": 2,
    "list_filter": 2,
    "list_reduce": 3,
    # objects
    "obj_get": 2,
    "obj_get_or": 3,
    "obj_has": 2,
    "obj_set": 3,
    "obj_del": 2,
    "obj_keys": 1,
    "obj_merge": 2,
    # effects
    "print": 1,
    "http_get": 1,
}

RESERVED_NAMES = {"result"}


@dataclass(frozen=True)
class Issue:
    pointer: str
    code: str
    message: str
    severity: str = "error"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pointer": self.pointer,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True)
class Flow:
    definite: Set[str]
    maybe: Set[str]


def _collect_user_arities(module: Dict[str, Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for fn in module.get("functions", []) or []:
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        params = fn.get("params", []) or []
        if isinstance(name, str) and isinstance(params, list):
            out[name] = len([p for p in params if isinstance(p, str)])
    return out


def _analyze_expr(expr: Any, ptr: List[Any], flow: Flow, issues: List[Issue], known_arities: Dict[str, int]) -> None:
    if isinstance(expr, (int, float, str, bool)) or expr is None:
        return
    if not isinstance(expr, dict):
        issues.append(Issue(join_pointer(ptr), "InvalidExpr", f"Expression must be literal or object, got {type(expr).__name__}"))
        return

    if "var" in expr:
        name = expr.get("var")
        if not isinstance(name, str):
            issues.append(Issue(join_pointer(ptr + ["var"]), "InvalidVarRef", "var must be a string"))
            return
        if name not in flow.definite:
            issues.append(Issue(join_pointer(ptr + ["var"]), "UndefinedVariable", f"Undefined variable: {name}"))
        return

    if "call" in expr:
        call = expr.get("call")
        if not isinstance(call, dict):
            issues.append(Issue(join_pointer(ptr + ["call"]), "InvalidCall", "call must be an object"))
            return
        fn = call.get("fn")
        args = call.get("args", [])
        if not isinstance(fn, str):
            issues.append(Issue(join_pointer(ptr + ["call", "fn"]), "InvalidCall", "call.fn must be a string"))
            return
        if not isinstance(args, list):
            issues.append(Issue(join_pointer(ptr + ["call", "args"]), "InvalidCall", "call.args must be an array"))
            return

        # name/arity checks
        fn_last = _qual_last(fn)
        if fn_last not in known_arities:
            issues.append(Issue(join_pointer(ptr + ["call", "fn"]), "UnknownFunctionCall", f"Unknown function: {fn}"))
        else:
            expected = known_arities[fn_last]
            if expected != len(args):
                issues.append(Issue(join_pointer(ptr + ["call"]), "ArityMismatch", f"{fn} expects {expected} args but got {len(args)}"))

        # higher-order builtins: validate referenced function name/arity when it's a string literal
        if fn_last in {"list_map", "list_filter", "list_reduce"} and args:
            ref = args[0]
            if isinstance(ref, str):
                ref_last = _qual_last(ref)
                if ref_last not in known_arities:
                    issues.append(Issue(join_pointer(ptr + ["call", "args", 0]), "UnknownFunctionRef", f"Unknown function reference: {ref}"))
                else:
                    want = 1 if fn_last in {"list_map", "list_filter"} else 2
                    got = known_arities[ref_last]
                    if got != want:
                        issues.append(Issue(join_pointer(ptr + ["call", "args", 0]), "ArityMismatch", f"{fn_last} expects '{ref}' to have arity {want} but it has {got}"))
            else:
                issues.append(Issue(join_pointer(ptr + ["call", "args", 0]), "InvalidFunctionRef", f"{fn_last} expects first arg to be a string function name"))

        for i, a in enumerate(args):
            _analyze_expr(a, ptr + ["call", "args", i], flow, issues, known_arities)
        return

    if "list" in expr:
        arr = expr.get("list")
        if not isinstance(arr, list):
            issues.append(Issue(join_pointer(ptr + ["list"]), "InvalidList", "list must be an array"))
            return
        for i, a in enumerate(arr):
            _analyze_expr(a, ptr + ["list", i], flow, issues, known_arities)
        return

    if "obj" in expr:
        obj = expr.get("obj")
        if not isinstance(obj, dict):
            issues.append(Issue(join_pointer(ptr + ["obj"]), "InvalidObj", "obj must be an object"))
            return
        for k, v in obj.items():
            _analyze_expr(v, ptr + ["obj", k], flow, issues, known_arities)
        return

    issues.append(Issue(join_pointer(ptr), "UnknownExpr", f"Unknown expr form: {list(expr.keys())}"))


def _analyze_block(stmts: List[Any], ptr: List[Any], flow_in: Flow, issues: List[Issue], known_arities: Dict[str, int]) -> Tuple[Flow, bool]:
    flow = Flow(set(flow_in.definite), set(flow_in.maybe))
    terminated = False

    for i, stmt in enumerate(stmts):
        stmt_ptr = ptr + [i]
        if terminated:
            issues.append(Issue(join_pointer(stmt_ptr), "UnreachableStatement", "Statement is unreachable (previous statement always returns).", "warning"))
            continue

        flow, always_returns = _analyze_stmt(stmt, stmt_ptr, flow, issues, known_arities)
        if always_returns:
            terminated = True

    return flow, terminated


def _analyze_stmt(stmt: Any, ptr: List[Any], flow_in: Flow, issues: List[Issue], known_arities: Dict[str, int]) -> Tuple[Flow, bool]:
    if not isinstance(stmt, dict) or len(stmt.keys()) != 1:
        issues.append(Issue(join_pointer(ptr), "InvalidStmt", "Statement must be an object with exactly one key"))
        return flow_in, False

    tag = next(iter(stmt.keys()))

    if tag == "let":
        spec = stmt["let"]
        if not isinstance(spec, dict):
            issues.append(Issue(join_pointer(ptr + ["let"]), "InvalidLet", "let must be an object"))
            return flow_in, False

        name = spec.get("name")
        if not isinstance(name, str):
            issues.append(Issue(join_pointer(ptr + ["let", "name"]), "InvalidLetName", "let.name must be a string"))
            return flow_in, False

        if name in RESERVED_NAMES:
            issues.append(Issue(join_pointer(ptr + ["let", "name"]), "ReservedName", f"'{name}' is reserved", "error"))

        if name in flow_in.maybe:
            issues.append(Issue(join_pointer(ptr + ["let", "name"]), "Rebind", f"'{name}' is already defined on some path"))

        _analyze_expr(spec.get("expr"), ptr + ["let", "expr"], flow_in, issues, known_arities)

        new_def = set(flow_in.definite)
        new_maybe = set(flow_in.maybe)
        new_def.add(name)
        new_maybe.add(name)
        return Flow(new_def, new_maybe), False

    if tag == "expr":
        _analyze_expr(stmt["expr"], ptr + ["expr"], flow_in, issues, known_arities)
        return flow_in, False

    if tag == "assert":
        spec = stmt["assert"]
        if not isinstance(spec, dict):
            issues.append(Issue(join_pointer(ptr + ["assert"]), "InvalidAssert", "assert must be an object"))
            return flow_in, False
        _analyze_expr(spec.get("expr"), ptr + ["assert", "expr"], flow_in, issues, known_arities)
        return flow_in, False

    if tag == "return":
        _analyze_expr(stmt["return"], ptr + ["return"], flow_in, issues, known_arities)
        return flow_in, True

    if tag == "if":
        spec = stmt["if"]
        if not isinstance(spec, dict):
            issues.append(Issue(join_pointer(ptr + ["if"]), "InvalidIf", "if must be an object"))
            return flow_in, False

        _analyze_expr(spec.get("cond"), ptr + ["if", "cond"], flow_in, issues, known_arities)

        then = spec.get("then", [])
        els = spec.get("else", [])
        if not isinstance(then, list) or not isinstance(els, list):
            issues.append(Issue(join_pointer(ptr + ["if"]), "InvalidIf", "if.then and if.else must be arrays"))
            return flow_in, False

        flow_then, ret_then = _analyze_block(then, ptr + ["if", "then"], flow_in, issues, known_arities)
        flow_else, ret_else = _analyze_block(els, ptr + ["if", "else"], flow_in, issues, known_arities)

        definite = flow_then.definite & flow_else.definite
        maybe = flow_then.maybe | flow_else.maybe
        return Flow(definite, maybe), (ret_then and ret_else)

    issues.append(Issue(join_pointer(ptr), "UnknownStmt", f"Unknown statement form: {tag}"))
    return flow_in, False


def check_module(module: Dict[str, Any]) -> List[Dict[str, Any]]:
    issues: List[Issue] = []

    user_arities = _collect_user_arities(module)
    known_arities = dict(BUILTIN_ARITY)
    known_arities.update(user_arities)

    for fi, fn in enumerate(module.get("functions", []) or []):
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        params = fn.get("params", []) or []
        if not isinstance(name, str) or not isinstance(params, list):
            continue

        # reserved param names
        for pi, p in enumerate(params):
            if p in RESERVED_NAMES:
                issues.append(Issue(join_pointer(["functions", fi, "params", pi]), "ReservedName", f"'{p}' is reserved"))

        flow0 = Flow(definite=set([p for p in params if isinstance(p, str)]), maybe=set([p for p in params if isinstance(p, str)]))
        body = fn.get("body", []) or []
        if not isinstance(body, list):
            issues.append(Issue(join_pointer(["functions", fi, "body"]), "InvalidBody", "body must be an array"))
            continue
        _, always_returns = _analyze_block(body, ["functions", fi, "body"], flow0, issues, known_arities)
        if not always_returns:
            issues.append(Issue(join_pointer(["functions", fi]), "MissingReturn", f"Function '{name}' may fall through without returning"))

    return [i.to_dict() for i in issues]


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="astra semantic")
    ap.add_argument("path", help="Path to Astra module JSON")
    ap.add_argument("--json", action="store_true", help="Emit issues as JSON")
    ap.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    args = ap.parse_args(argv)

    try:
        module = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read/parse JSON: {e}", file=sys.stderr)
        return 3

    issues = check_module(module)
    if args.json:
        print(json.dumps(issues, indent=2, ensure_ascii=False))
    else:
        for i in issues:
            print(f"{i['severity']} {i['code']} {i['pointer']}: {i['message']}")

    has_errors = any(i["severity"] == "error" for i in issues)
    has_warnings = any(i["severity"] == "warning" for i in issues)

    if has_errors or (args.strict and has_warnings):
        return 2
    if has_warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
