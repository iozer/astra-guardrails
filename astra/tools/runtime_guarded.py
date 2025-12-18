"""Effect-guarded runtime builtins for Astra.

This runtime is used by:
- the AST sandbox interpreter
- the Python codegen sandbox

The goal is NOT to sandbox Python completely (which is very hard), but to:
- ensure all side-effectful operations go through explicit gates
- provide deterministic, testable builtins

Effect model:
- The host sets an allowlist via `set_allowed_effects([...])`.
- Side-effect builtins call `require("io.print")`, `require("net.http")`, etc.

Higher-order builtins (list_map/filter/reduce):
- These need to invoke a user-defined function or builtin by name.
- The host must install a dispatcher via `set_dispatch(fn)`.

Builtin names are strings to avoid Python keyword conflicts (e.g., "and").
"""

from __future__ import annotations

import urllib.request
from typing import Any, Callable, Dict, List, Optional, Set


class EffectError(RuntimeError):
    pass


_allowed_effects: Set[str] = {"pure"}


def set_allowed_effects(effects: List[str]) -> None:
    global _allowed_effects
    _allowed_effects = set(effects or [])
    if not _allowed_effects:
        _allowed_effects = {"pure"}


def require(effect: str) -> None:
    if effect not in _allowed_effects:
        raise EffectError(f"Effect '{effect}' is not allowed (allowed={sorted(_allowed_effects)})")


# -------------------------
# Optional dispatcher (for higher-order builtins)
# -------------------------

# Signature: dispatch(fn_name, args_list) -> value
_dispatch: Optional[Callable[[str, List[Any]], Any]] = None


def set_dispatch(dispatch: Optional[Callable[[str, List[Any]], Any]]) -> None:
    """Install a function dispatcher used by higher-order builtins.

    The dispatcher receives the *Astra* function name (possibly qualified) and a
    list of positional arguments.

    The dispatcher MUST enforce effects via `require(...)` at builtin boundaries.
    """
    global _dispatch
    _dispatch = dispatch


def _need_dispatch() -> Callable[[str, List[Any]], Any]:
    if _dispatch is None:
        raise RuntimeError(
            "Higher-order builtin requires a dispatcher; call rt.set_dispatch(...) from the host"
        )
    return _dispatch


# -------------------------
# Builtins (pure)
# -------------------------

# math / comparisons


def add(a: Any, b: Any) -> Any:
    return a + b


def sub(a: Any, b: Any) -> Any:
    return a - b


def mul(a: Any, b: Any) -> Any:
    return a * b


def div(a: Any, b: Any) -> Any:
    return a / b


def eq(a: Any, b: Any) -> bool:
    return a == b


def neq(a: Any, b: Any) -> bool:
    return a != b


def lt(a: Any, b: Any) -> bool:
    return a < b


def lte(a: Any, b: Any) -> bool:
    return a <= b


def gt(a: Any, b: Any) -> bool:
    return a > b


def gte(a: Any, b: Any) -> bool:
    return a >= b


def _and(a: Any, b: Any) -> bool:
    return bool(a) and bool(b)


def _or(a: Any, b: Any) -> bool:
    return bool(a) or bool(b)


def _not(a: Any) -> bool:
    return not bool(a)


# strings


def str_len(s: str) -> int:
    return len(s)


def str_concat(a: str, b: str) -> str:
    return a + b


def str_contains(s: str, sub: str) -> bool:
    return sub in s


# lists


def length(xs: Any) -> int:
    return len(xs)


def list_get(xs: List[Any], i: int) -> Any:
    return xs[i]


def list_set(xs: List[Any], i: int, v: Any) -> List[Any]:
    ys = list(xs)
    ys[i] = v
    return ys


def list_append(xs: List[Any], v: Any) -> List[Any]:
    return list(xs) + [v]


def list_concat(xs: List[Any], ys: List[Any]) -> List[Any]:
    return list(xs) + list(ys)


def list_slice(xs: List[Any], start: Any, end: Any) -> List[Any]:
    # start/end may be null
    s = None if start is None else int(start)
    e = None if end is None else int(end)
    return list(xs)[s:e]


def list_range(n: int) -> List[int]:
    return list(range(int(n)))


def list_sum(xs: List[Any]) -> Any:
    return sum(xs)


def list_mean(xs: List[Any]) -> float:
    if len(xs) == 0:
        raise ValueError("list_mean: empty list")
    return float(sum(xs)) / float(len(xs))


def list_map(fn: str, xs: List[Any]) -> List[Any]:
    disp = _need_dispatch()
    out: List[Any] = []
    for x in xs:
        out.append(disp(fn, [x]))
    return out


def list_filter(fn: str, xs: List[Any]) -> List[Any]:
    disp = _need_dispatch()
    out: List[Any] = []
    for x in xs:
        if disp(fn, [x]):
            out.append(x)
    return out


def list_reduce(fn: str, init: Any, xs: List[Any]) -> Any:
    disp = _need_dispatch()
    acc = init
    for x in xs:
        acc = disp(fn, [acc, x])
    return acc


# objects (records)


def obj_get(o: Dict[str, Any], key: str) -> Any:
    return o[key]


def obj_get_or(o: Dict[str, Any], key: str, default: Any) -> Any:
    return o.get(key, default)


def obj_has(o: Dict[str, Any], key: str) -> bool:
    return key in o


def obj_set(o: Dict[str, Any], key: str, value: Any) -> Dict[str, Any]:
    out = dict(o)
    out[key] = value
    return out


def obj_del(o: Dict[str, Any], key: str) -> Dict[str, Any]:
    if key not in o:
        return dict(o)
    out = dict(o)
    del out[key]
    return out


def obj_keys(o: Dict[str, Any]) -> List[str]:
    # stable order for deterministic behavior
    return sorted(list(o.keys()))


def obj_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    out.update(b)
    return out


# -------------------------
# Builtins (effects)
# -------------------------


def builtin_print(x: Any) -> None:
    require("io.print")
    print(x)
    return None


def http_get(url: str) -> str:
    require("net.http")
    with urllib.request.urlopen(url) as resp:
        data = resp.read()
    try:
        return data.decode("utf-8")
    except Exception:
        # best-effort
        return data.decode("latin-1", errors="replace")


BUILTINS: Dict[str, Callable[..., Any]] = {
    # arithmetic
    "add": add,
    "sub": sub,
    "mul": mul,
    "div": div,
    # comparisons
    "eq": eq,
    "neq": neq,
    "lt": lt,
    "lte": lte,
    "gt": gt,
    "gte": gte,
    # boolean
    "and": _and,
    "or": _or,
    "not": _not,
    # strings
    "str_len": str_len,
    "str_concat": str_concat,
    "str_contains": str_contains,
    # lists
    "len": length,
    "list_get": list_get,
    "list_set": list_set,
    "list_append": list_append,
    "list_concat": list_concat,
    "list_slice": list_slice,
    "list_range": list_range,
    "list_sum": list_sum,
    "list_mean": list_mean,
    "list_map": list_map,
    "list_filter": list_filter,
    "list_reduce": list_reduce,
    # objects
    "obj_get": obj_get,
    "obj_get_or": obj_get_or,
    "obj_has": obj_has,
    "obj_set": obj_set,
    "obj_del": obj_del,
    "obj_keys": obj_keys,
    "obj_merge": obj_merge,
    # side effects
    "print": builtin_print,
    "http_get": http_get,
}


def call_builtin(name: str, args: List[Any]) -> Any:
    if name not in BUILTINS:
        raise KeyError(f"Unknown builtin: {name}")
    return BUILTINS[name](*args)
