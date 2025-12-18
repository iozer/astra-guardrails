"""Property-based testing for Astra with shrinking.

This runner can:
- Execute property tests embedded in a module under `properties` (schema v1.0)
- Or run ad-hoc fuzzing for a specific function

Supported generators/shrinkers:
- Int
- Bool
- String (simple)
- List[T] (T supported types)
- Record{...}

Execution uses the AST sandbox (`sandbox_ast`) and therefore enforces effects via
`runtime_guarded`.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from astra.tools import sandbox_ast
from astra.tools import runtime_guarded as rt
from astra.tools.typecheck import AnyType, ListType, Prim, RecordType, Type, Var, parse_type_expr


# -------------------------
# Generators
# -------------------------

def _gen_int(rnd: random.Random, max_size: int) -> int:
    # bounded ints for determinism
    bound = max(1, min(10_000, max_size * 50 + 50))
    return rnd.randint(-bound, bound)


def _gen_bool(rnd: random.Random, max_size: int) -> bool:
    return bool(rnd.randint(0, 1))


def _gen_string(rnd: random.Random, max_size: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    n = rnd.randint(0, min(30, max_size))
    return "".join(rnd.choice(alphabet) for _ in range(n))


def _subst_typevars(t: Type, mapping: Dict[str, Type]) -> Type:
    # Type in typecheck.py is immutable dataclasses; substitute Var names.
    if isinstance(t, Var):
        return mapping.get(t.name.split("#")[0], Prim("Int"))
    if isinstance(t, ListType):
        return ListType(_subst_typevars(t.elem, mapping))
    if isinstance(t, RecordType):
        return RecordType({k: _subst_typevars(v, mapping) for k, v in t.fields.items()})
    return t


def gen_value(t: Type, rnd: random.Random, max_size: int, *, typevar_defaults: Optional[Dict[str, Type]] = None) -> Any:
    typevar_defaults = typevar_defaults or {}
    t = _subst_typevars(t, typevar_defaults)

    if isinstance(t, AnyType):
        return _gen_int(rnd, max_size)
    if isinstance(t, Prim):
        if t.name == "Int":
            return _gen_int(rnd, max_size)
        if t.name == "Bool":
            return _gen_bool(rnd, max_size)
        if t.name == "String":
            return _gen_string(rnd, max_size)
        if t.name == "Float":
            return float(_gen_int(rnd, max_size)) / 10.0
        if t.name == "Null":
            return None
        return _gen_int(rnd, max_size)
    if isinstance(t, ListType):
        n = rnd.randint(0, min(max_size, 50))
        return [gen_value(t.elem, rnd, max_size, typevar_defaults=typevar_defaults) for _ in range(n)]
    if isinstance(t, RecordType):
        return {k: gen_value(v, rnd, max_size, typevar_defaults=typevar_defaults) for k, v in t.fields.items()}
    if isinstance(t, Var):
        return gen_value(typevar_defaults.get(t.name, Prim("Int")), rnd, max_size, typevar_defaults=typevar_defaults)
    return _gen_int(rnd, max_size)


# -------------------------
# Shrinking
# -------------------------

def shrink_int(n: int) -> Iterable[int]:
    if n == 0:
        return
    yield 0
    yield 1
    yield -1
    # move toward 0
    cur = n
    while cur != 0:
        cur = int(cur / 2)
        if cur != 0:
            yield cur


def shrink_list(xs: List[Any]) -> Iterable[List[Any]]:
    # empty
    if xs:
        yield []
    # halves
    n = len(xs)
    if n >= 2:
        yield xs[: n // 2]
        yield xs[n // 2 :]
    # drop each element
    for i in range(len(xs)):
        yield xs[:i] + xs[i + 1 :]


def shrink_value(t: Type, v: Any, *, typevar_defaults: Optional[Dict[str, Type]] = None) -> Iterable[Any]:
    typevar_defaults = typevar_defaults or {}
    t = _subst_typevars(t, typevar_defaults)

    if isinstance(t, AnyType):
        if isinstance(v, int):
            yield from shrink_int(v)
        return

    if isinstance(t, Prim):
        if t.name == "Int" and isinstance(v, int):
            yield from shrink_int(v)
        if t.name == "Bool" and isinstance(v, bool):
            if v:
                yield False
        if t.name == "String" and isinstance(v, str):
            if v:
                yield ""
                yield v[: len(v) // 2]
        return

    if isinstance(t, ListType) and isinstance(v, list):
        # shrink structure
        yield from shrink_list(v)
        # shrink elements
        for i, elem in enumerate(v):
            for cand in shrink_value(t.elem, elem, typevar_defaults=typevar_defaults):
                vv = list(v)
                vv[i] = cand
                yield vv
        return

    if isinstance(t, RecordType) and isinstance(v, dict):
        for k, ft in t.fields.items():
            if k not in v:
                continue
            for cand in shrink_value(ft, v[k], typevar_defaults=typevar_defaults):
                vv = dict(v)
                vv[k] = cand
                yield vv
        return

    if isinstance(t, Var):
        yield from shrink_value(typevar_defaults.get(t.name, Prim("Int")), v, typevar_defaults=typevar_defaults)
        return


# -------------------------
# Runner
# -------------------------

def eval_post_expr(post: Any, env: Dict[str, Any], module: Dict[str, Any]) -> bool:
    # Use sandbox evaluator; build fns mapping
    fns: Dict[str, Dict[str, Any]] = {}
    for f in module.get("functions", []) or []:
        if isinstance(f, dict) and isinstance(f.get("name"), str):
            fns[f["name"]] = f
    val = sandbox_ast.eval_expr(post, env, fns)
    return bool(val)


def minimize_failure(
    module: Dict[str, Any],
    fn_name: str,
    param_names: List[str],
    param_types: List[Type],
    args: List[Any],
    post_expr: Any,
    allowed_effects: List[str],
    *,
    typevar_defaults: Optional[Dict[str, Type]] = None,
) -> List[Any]:
    """Greedy shrinker: minimize each arg while the failure persists."""
    typevar_defaults = typevar_defaults or {}

    def fails(candidate_args: List[Any]) -> bool:
        try:
            result = sandbox_ast.run_module(module, fn_name, candidate_args, allowed_effects)
            env = {p: a for p, a in zip(param_names, candidate_args)}
            env["result"] = result
            ok = eval_post_expr(post_expr, env, module)
            return not ok
        except Exception:
            return True

    cur = list(args)
    changed = True
    while changed:
        changed = False
        for i in range(len(cur)):
            t = param_types[i] if i < len(param_types) else AnyType()
            for cand in shrink_value(t, cur[i], typevar_defaults=typevar_defaults):
                trial = list(cur)
                trial[i] = cand
                if fails(trial):
                    cur = trial
                    changed = True
                    break
    return cur


@dataclass
class PropResult:
    ok: bool
    cases: int
    failing_args: Optional[List[Any]] = None
    minimized_args: Optional[List[Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "cases": self.cases,
            "failing_args": self.failing_args,
            "minimized_args": self.minimized_args,
            "error": self.error,
        }


def run_property_for_function(
    module: Dict[str, Any],
    fn: Dict[str, Any],
    post_expr: Any,
    *,
    cases: int,
    seed: Optional[int],
    max_size: int,
    allowed_effects: List[str],
) -> PropResult:
    name = fn.get("name")
    if not isinstance(name, str):
        return PropResult(False, 0, error="invalid function")

    params = [p for p in (fn.get("params", []) or []) if isinstance(p, str)]
    pt_raw = fn.get("param_types")
    if isinstance(pt_raw, list) and len(pt_raw) == len(params):
        param_types = [parse_type_expr(t) for t in pt_raw]
    else:
        param_types = [AnyType() for _ in params]

    # generics default mapping: all type params -> Int
    type_params = [tp for tp in (fn.get("type_params") or []) if isinstance(tp, str)]
    defaults = {tp: Prim("Int") for tp in type_params}

    rnd = random.Random(seed)
    rt.set_allowed_effects(allowed_effects)

    for i in range(cases):
        args = [gen_value(t, rnd, max_size, typevar_defaults=defaults) for t in param_types]
        try:
            result = sandbox_ast.run_module(module, name, args, allowed_effects)
            env = {p: a for p, a in zip(params, args)}
            env["result"] = result
            ok = eval_post_expr(post_expr, env, module)
            if not ok:
                minimized = minimize_failure(module, name, params, param_types, args, post_expr, allowed_effects, typevar_defaults=defaults)
                return PropResult(False, i + 1, failing_args=args, minimized_args=minimized, error="postcondition failed")
        except Exception as e:
            minimized = minimize_failure(module, name, params, param_types, args, post_expr, allowed_effects, typevar_defaults=defaults)
            return PropResult(False, i + 1, failing_args=args, minimized_args=minimized, error=str(e))

    return PropResult(True, cases)


def run_module_properties(module: Dict[str, Any], allowed_effects: List[str]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    # index functions
    fns = {f.get("name"): f for f in (module.get("functions", []) or []) if isinstance(f, dict)}
    for ptest in module.get("properties", []) or []:
        if not isinstance(ptest, dict):
            continue
        name = ptest.get("name")
        fn_name = ptest.get("fn")
        strat = ptest.get("strategy", {}) or {}
        expect = ptest.get("expect", {}) or {}
        post = expect.get("post")
        if not isinstance(fn_name, str) or post is None:
            continue
        fn = fns.get(fn_name)
        if not isinstance(fn, dict):
            results.append({"name": name, "fn": fn_name, "ok": False, "error": "unknown function"})
            continue
        cases = int(strat.get("cases", 100))
        seed = strat.get("seed")
        max_size = int(strat.get("max_size", 20))
        res = run_property_for_function(module, fn, post, cases=cases, seed=seed if isinstance(seed, int) else None, max_size=max_size, allowed_effects=allowed_effects)
        d = res.to_dict()
        d["name"] = name
        d["fn"] = fn_name
        results.append(d)
    return results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="astra prop")
    ap.add_argument("path", help="Path to Astra module JSON")
    ap.add_argument("--fn", help="Run ad-hoc property: function name")
    ap.add_argument("--post", help="JSON-encoded Astra expression for postcondition")
    ap.add_argument("--cases", type=int, default=200)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--max-size", type=int, default=20)
    ap.add_argument("--allowed", nargs="*", default=["pure"], help="Allowed effects")
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    args = ap.parse_args(argv)

    try:
        module = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read/parse JSON: {e}", file=sys.stderr)
        return 3

    if args.fn and args.post:
        # ad-hoc
        fn = None
        for f in module.get("functions", []) or []:
            if isinstance(f, dict) and f.get("name") == args.fn:
                fn = f
                break
        if fn is None:
            print(f"Unknown function: {args.fn}", file=sys.stderr)
            return 2
        post_expr = json.loads(args.post)
        res = run_property_for_function(module, fn, post_expr, cases=args.cases, seed=args.seed, max_size=args.max_size, allowed_effects=args.allowed)
        out = res.to_dict()
        out["fn"] = args.fn
        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
        else:
            print(out)
        return 0 if res.ok else 2

    # embedded properties
    results = run_module_properties(module, args.allowed)
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(r)
    ok = all(r.get("ok") for r in results) if results else True
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
