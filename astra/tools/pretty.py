import json
import re
from typing import Any, Dict, List


def _indent(lines: List[str], n: int) -> List[str]:
    pref = "  " * n
    return [pref + l for l in lines]


def _is_ident(s: str) -> bool:
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", s) is not None


def _qual_last(name: Any) -> str:
    if not isinstance(name, str):
        return str(name)
    return name.rsplit(".", 1)[-1]


def _expr(expr: Any) -> str:
    if expr is None:
        return "null"
    if isinstance(expr, bool):
        return "true" if expr else "false"
    if isinstance(expr, (int, float)) and not isinstance(expr, bool):
        return str(expr)
    if isinstance(expr, str):
        return json.dumps(expr, ensure_ascii=False)

    if not isinstance(expr, dict):
        return "<invalid>"

    if "var" in expr:
        return expr["var"]

    if "call" in expr:
        call = expr["call"]
        fn = call.get("fn", "?")
        args = call.get("args", []) or []
        fn_last = _qual_last(fn)

        # Ergonomic sugar for records: obj_get(x, "field") prints as x.field
        if fn_last == "obj_get" and isinstance(args, list) and len(args) == 2 and isinstance(args[1], str) and _is_ident(args[1]):
            return f"{_expr(args[0])}.{args[1]}"

        return f"{fn}({', '.join(_expr(a) for a in args)})"

    if "list" in expr:
        return "[" + ", ".join(_expr(a) for a in expr["list"]) + "]"

    if "obj" in expr:
        obj = expr["obj"]
        items = ", ".join(f"{k}: {_expr(v)}" for k, v in obj.items())
        return "{" + items + "}"

    return "<expr>"


def _stmt(stmt: Any, indent: int = 0) -> List[str]:
    if not isinstance(stmt, dict) or len(stmt.keys()) != 1:
        return _indent(["<invalid stmt>"], indent)
    tag = next(iter(stmt.keys()))
    val = stmt[tag]

    if tag == "let":
        name = val.get("name", "_")
        return _indent([f"let {name} = {_expr(val.get('expr'))}"], indent)

    if tag == "expr":
        return _indent([_expr(val)], indent)

    if tag == "assert":
        msg = val.get("message")
        if isinstance(msg, str):
            return _indent([f"assert {_expr(val.get('expr'))} : {json.dumps(msg, ensure_ascii=False)}"], indent)
        return _indent([f"assert {_expr(val.get('expr'))}"], indent)

    if tag == "return":
        return _indent([f"return {_expr(val)}"], indent)

    if tag == "if":
        cond = _expr(val.get("cond"))
        then = val.get("then", []) or []
        els = val.get("else", []) or []
        out: List[str] = []
        out.extend(_indent([f"if {cond}:"], indent))
        for s in then:
            out.extend(_stmt(s, indent + 1))
        if els:
            out.extend(_indent(["else:"], indent))
            for s in els:
                out.extend(_stmt(s, indent + 1))
        return out

    return _indent([f"<unknown stmt: {tag}>"], indent)


def pretty_module(mod: Dict[str, Any]) -> str:
    out: List[str] = []

    if mod.get("schema"):
        out.append(f"schema: {mod.get('schema')}")
    if mod.get("version"):
        out.append(f"version: {mod.get('version')}")
    if mod.get("module"):
        out.append(f"module: {mod.get('module')}")
    if mod.get("effects"):
        out.append(f"effects: {', '.join(mod.get('effects') or [])}")
    if mod.get("imports"):
        out.append(f"imports: {', '.join(mod.get('imports') or [])}")

    for fn in mod.get("functions", []) or []:
        name = fn.get("name")
        params = fn.get("params") or []
        ret = fn.get("returns") or "Any"
        effects = fn.get("effects") or []
        header = f"func {name}({', '.join(params)}) -> {ret}"
        if effects:
            header += f" effects[{', '.join(effects)}]"
        out.append("")
        out.append(header)
        out.append("{")
        for s in fn.get("body") or []:
            out.extend(_stmt(s, 1))
        out.append("}")

    return "\n".join(out) + "\n"
