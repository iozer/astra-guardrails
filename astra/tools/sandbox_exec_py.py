"""Execute Astra module by compiling to Python and running in a restricted environment.

Security model (pragmatic):
- Generated code contains no import statements (by default in this runner).
- `exec` runs with a restricted `__builtins__` dictionary.
- Side effects are enforced by `runtime_guarded` allowlist.

This is NOT a perfect sandbox, but combined with:
- strict schema
- semantic/type/effect checks
- limited expression/statement forms
it provides a practical execution path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from astra.tools import runtime_guarded as rt
from astra.tools.codegen_py import generate_python
from astra.tools.sandbox_ast import run_module as run_ast  # for fallback


def _qual_last(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def run_python_sandbox(module: Dict[str, Any], fn: str, args: List[Any], allowed_effects: List[str]) -> Any:
    rt.set_allowed_effects(allowed_effects)

    code = generate_python(module, standalone=False)

    # Very restricted builtins
    safe_builtins: Dict[str, Any] = {
        "True": True,
        "False": False,
        "None": None,
        "list": list,
        "globals": globals,
        "AssertionError": AssertionError,
        "NameError": NameError,
        "TypeError": TypeError,
        "ValueError": ValueError,
    }

    g: Dict[str, Any] = {
        "__builtins__": safe_builtins,
        "rt": rt,
    }

    exec(compile(code, "<astra>", "exec"), g, g)

    fn_last = _qual_last(fn)
    if fn_last in rt.BUILTINS:
        return rt.call_builtin(fn_last, args)

    f = g.get(fn_last)
    if f is None:
        raise NameError(f"Unknown function: {fn}")
    return f(*args)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="astra run-py")
    ap.add_argument("path", help="Path to Astra module JSON")
    ap.add_argument("--fn", required=True, help="Function name")
    ap.add_argument("--args", nargs="*", default=[], help="Positional args (JSON literals)")
    ap.add_argument("--allowed", nargs="*", default=["pure"], help="Allowed effects")
    ap.add_argument("--fallback-ast", action="store_true", help="On sandbox error, fallback to AST interpreter")
    args_ns = ap.parse_args(argv)

    try:
        module = json.loads(Path(args_ns.path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read/parse JSON: {e}", file=sys.stderr)
        return 3

    parsed_args: List[Any] = []
    for a in args_ns.args:
        try:
            parsed_args.append(json.loads(a))
        except Exception:
            parsed_args.append(a)

    try:
        out = run_python_sandbox(module, args_ns.fn, parsed_args, args_ns.allowed)
    except Exception as e:
        if args_ns.fallback_ast:
            try:
                out = run_ast(module, args_ns.fn, parsed_args, args_ns.allowed)
            except Exception as e2:
                print(f"error: {e2}", file=sys.stderr)
                return 2
        else:
            print(f"error: {e}", file=sys.stderr)
            return 2

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
