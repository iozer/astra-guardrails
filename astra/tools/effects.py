"""Static effect (capability) checker for Astra.

The effect system is declarative:
- Each function declares a set of effects in `effects`.
- Builtins declare effects.
- A function's declared effects must be a superset of effects required by
  everything it calls (transitively).

This module is used by:
- CLI (`astra effectcheck`)
- LSP diagnostics
- Repair loop

Runtime enforcement is provided by `astra.tools.runtime_guarded`.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple


def _qual_last(name: str) -> str:
    return name.rsplit(".", 1)[-1]

# Builtins: name -> (arity, required effects)
# NOTE: 'pure' means no side effects.
BUILTIN_EFFECTS: Dict[str, Tuple[int, Set[str]]] = {
    # arithmetic
    "add": (2, {"pure"}),
    "sub": (2, {"pure"}),
    "mul": (2, {"pure"}),
    "div": (2, {"pure"}),
    # comparisons
    "eq": (2, {"pure"}),
    "neq": (2, {"pure"}),
    "lt": (2, {"pure"}),
    "lte": (2, {"pure"}),
    "gt": (2, {"pure"}),
    "gte": (2, {"pure"}),
    # boolean
    "and": (2, {"pure"}),
    "or": (2, {"pure"}),
    "not": (1, {"pure"}),
    # strings
    "str_len": (1, {"pure"}),
    "str_concat": (2, {"pure"}),
    "str_contains": (2, {"pure"}),
    # lists
    "len": (1, {"pure"}),
    "list_get": (2, {"pure"}),
    "list_set": (3, {"pure"}),
    "list_append": (2, {"pure"}),
    "list_concat": (2, {"pure"}),
    "list_slice": (3, {"pure"}),
    "list_range": (1, {"pure"}),
    "list_sum": (1, {"pure"}),
    "list_mean": (1, {"pure"}),
    "list_map": (2, {"pure"}),
    "list_filter": (2, {"pure"}),
    "list_reduce": (3, {"pure"}),
    # objects
    "obj_get": (2, {"pure"}),
    "obj_get_or": (3, {"pure"}),
    "obj_has": (2, {"pure"}),
    "obj_set": (3, {"pure"}),
    "obj_del": (2, {"pure"}),
    "obj_keys": (1, {"pure"}),
    "obj_merge": (2, {"pure"}),
    # effects
    "print": (1, {"io.print"}),
    "http_get": (1, {"net.http"}),
}


@dataclass
class EffectIssue:
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


def iter_calls(node: Any, ptr: str = "") -> Iterable[Tuple[str, str]]:
    """Yield (fn_name, pointer) for all call-like references inside node.

    - Normal calls yield the callee name (last segment if qualified).
    - Higher-order builtins (list_map/list_filter/list_reduce) also yield the referenced
      function name when the first arg is a string literal.
    """
    if isinstance(node, dict):
        if "call" in node and isinstance(node["call"], dict):
            call = node["call"]
            fn = call.get("fn")
            if isinstance(fn, str):
                fn_last = _qual_last(fn)
                yield fn_last, f"{ptr}/call/fn" if ptr else "/call/fn"

                # Higher-order builtins: first arg is a function name string.
                if fn_last in {"list_map", "list_filter", "list_reduce"}:
                    args = call.get("args", [])
                    if isinstance(args, list) and args:
                        callee = args[0]
                        if isinstance(callee, str):
                            yield _qual_last(callee), f"{ptr}/call/args/0" if ptr else "/call/args/0"

        for k, v in node.items():
            child_ptr = f"{ptr}/{k}" if ptr else f"/{k}"
            yield from iter_calls(v, child_ptr)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            child_ptr = f"{ptr}/{i}" if ptr else f"/{i}"
            yield from iter_calls(v, child_ptr)


def _user_function_index(module: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    funcs: Dict[str, Dict[str, Any]] = {}
    idx: Dict[str, int] = {}
    for i, f in enumerate(module.get("functions", []) or []):
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        if isinstance(name, str):
            funcs[name] = f
            idx[name] = i
    return funcs, idx


def compute_transitive_effects(module: Dict[str, Any]) -> Tuple[Dict[str, Set[str]], List[EffectIssue]]:
    """Return (effects_map, issues).

    effects_map maps function name -> transitive required effects.
    """
    issues: List[EffectIssue] = []
    user_funcs, func_index = _user_function_index(module)

    memo: Dict[str, Set[str]] = {}
    visiting: Set[str] = set()

    def visit(fn_name: str) -> Set[str]:
        if fn_name in memo:
            return memo[fn_name]
        if fn_name in visiting:
            # recursion: trust declaration to avoid infinite loops
            declared = set(user_funcs.get(fn_name, {}).get("effects", []) or [])
            memo[fn_name] = declared or {"pure"}
            return memo[fn_name]

        if fn_name in BUILTIN_EFFECTS:
            memo[fn_name] = set(BUILTIN_EFFECTS[fn_name][1])
            return memo[fn_name]

        fn = user_funcs.get(fn_name)
        if fn is None:
            memo[fn_name] = set()
            return set()

        visiting.add(fn_name)
        required: Set[str] = set(fn.get("effects", []) or []) or {"pure"}

        # traverse body calls
        fn_ptr = f"/functions/{func_index[fn_name]}/body"
        for stmt_i, stmt in enumerate(fn.get("body", []) or []):
            stmt_ptr = f"{fn_ptr}/{stmt_i}"
            for callee, callee_ptr in iter_calls(stmt, stmt_ptr):
                if callee in BUILTIN_EFFECTS or callee in user_funcs:
                    required |= visit(callee)
                else:
                    issues.append(
                        EffectIssue(
                            pointer=callee_ptr,
                            code="UnknownFunctionCall",
                            message=f"Call to unknown function: {callee}",
                            severity="error",
                        )
                    )

        visiting.remove(fn_name)
        memo[fn_name] = required
        return required

    for name in user_funcs.keys():
        visit(name)

    return memo, issues


def check_effects(module: Dict[str, Any]) -> List[Dict[str, Any]]:
    user_funcs, func_index = _user_function_index(module)
    effects_map, issues = compute_transitive_effects(module)

    for fn_name, required in effects_map.items():
        if fn_name in BUILTIN_EFFECTS:
            continue
        if fn_name not in user_funcs:
            continue
        idx = func_index[fn_name]
        declared = set(user_funcs[fn_name].get("effects", []) or []) or {"pure"}
        missing = required - declared
        if missing:
            issues.append(
                EffectIssue(
                    pointer=f"/functions/{idx}/effects",
                    code="MissingEffect",
                    message=f"Function '{fn_name}' requires effects {sorted(missing)} but declares {sorted(declared)}.",
                    severity="error",
                )
            )
        if "pure" in declared and len(declared) > 1:
            issues.append(
                EffectIssue(
                    pointer=f"/functions/{idx}/effects",
                    code="NotPure",
                    message=f"Function '{fn_name}' declares 'pure' but also {sorted(declared - {'pure'})}. Consider removing 'pure'.",
                    severity="warning",
                )
            )

    return [i.to_dict() for i in issues]


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="astra effectcheck")
    ap.add_argument("path", help="Path to Astra module JSON")
    ap.add_argument("--json", action="store_true", help="Emit issues as JSON")
    args = ap.parse_args(argv)

    try:
        module = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read/parse JSON: {e}", file=sys.stderr)
        return 3

    issues = check_effects(module)
    if args.json:
        print(json.dumps(issues, indent=2, ensure_ascii=False))
    else:
        for i in issues:
            print(f"{i['severity']} {i['code']} {i['pointer']}: {i['message']}")

    has_errors = any(i["severity"] == "error" for i in issues)
    has_warnings = any(i["severity"] == "warning" for i in issues)
    if has_errors:
        return 2
    if has_warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
