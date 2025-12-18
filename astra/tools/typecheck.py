"""Polymorphic generic type checker for Astra (v1.0).

Key features
- Primitive types: Int, Float, Bool, String, Null, Any
- Composite: List[T], Record{field:Type,...}
- Function generics: `type_params: ['T','U']` are instantiated per call site
- Local inference for `let` bindings

This is deliberately pragmatic (not full HM). It is designed to:
- be deterministic
- emit JSON pointer diagnostics for LLM repair loops
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from astra.tools.pointer import join_pointer


def _qual_last(name: str) -> str:
    return name.rsplit(".", 1)[-1]


# -------------------------
# Type model
# -------------------------

class Type:
    def render(self) -> str:
        raise NotImplementedError

    def __str__(self) -> str:
        return self.render()


@dataclass(frozen=True)
class AnyType(Type):
    def render(self) -> str:
        return "Any"


@dataclass(frozen=True)
class Prim(Type):
    name: str

    def render(self) -> str:
        return self.name


@dataclass(frozen=True)
class Var(Type):
    name: str

    def render(self) -> str:
        return self.name


@dataclass(frozen=True)
class ListT(Type):
    elem: Type

    def render(self) -> str:
        return f"List[{self.elem.render()}]"


@dataclass(frozen=True)
class RecordT(Type):
    fields: Dict[str, Type]

    def render(self) -> str:
        if not self.fields:
            return "Record{}"
        inside = ",".join(f"{k}:{v.render()}" for k, v in sorted(self.fields.items()))
        return f"Record{{{inside}}}"


PRIMS = {"Int", "Float", "Bool", "String", "Null", "Any"}

# Backwards-compatible aliases
ListType = ListT
RecordType = RecordT


# -------------------------
# Type parsing
# -------------------------

class _Tok:
    def __init__(self, kind: str, text: str) -> None:
        self.kind = kind
        self.text = text


def _tokenize(s: str) -> List[_Tok]:
    out: List[_Tok] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c in "[]{}:,":
            out.append(_Tok(c, c))
            i += 1
            continue
        # identifier
        j = i
        while j < len(s) and (s[j].isalnum() or s[j] == "_"):
            j += 1
        if j == i:
            raise ValueError(f"Invalid type char at {i}: {s[i:i+10]!r}")
        out.append(_Tok("IDENT", s[i:j]))
        i = j
    return out


class _Parser:
    def __init__(self, toks: List[_Tok]) -> None:
        self.toks = toks
        self.i = 0

    def peek(self) -> Optional[_Tok]:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def accept(self, kind: str) -> Optional[_Tok]:
        t = self.peek()
        if t is not None and t.kind == kind:
            self.i += 1
            return t
        return None

    def expect(self, kind: str) -> _Tok:
        t = self.peek()
        if t is None or t.kind != kind:
            raise ValueError(f"Expected {kind}, got {t.kind if t else 'EOF'}")
        self.i += 1
        return t

    def parse_type(self) -> Type:
        t = self.expect("IDENT")
        name = t.text
        if name == "List":
            self.expect("[")
            elem = self.parse_type()
            self.expect("]")
            return ListT(elem)
        if name == "Record":
            self.expect("{")
            fields: Dict[str, Type] = {}
            if self.accept("}"):
                return RecordT(fields)
            while True:
                k = self.expect("IDENT").text
                self.expect(":")
                v = self.parse_type()
                fields[k] = v
                if self.accept(","):
                    continue
                self.expect("}")
                break
            return RecordT(fields)
        if name in PRIMS:
            if name == "Any":
                return AnyType()
            return Prim(name)
        # type var
        return Var(name)


def parse_type_expr(expr: str) -> Type:
    toks = _tokenize(expr)
    p = _Parser(toks)
    ty = p.parse_type()
    if p.peek() is not None:
        raise ValueError(f"Unexpected tokens at end of type expr: {expr!r}")
    return ty


# -------------------------
# Unification / substitution
# -------------------------

Subst = Dict[str, Type]


def _apply(ty: Type, subst: Subst) -> Type:
    if isinstance(ty, Var) and ty.name in subst:
        return _apply(subst[ty.name], subst)
    if isinstance(ty, ListT):
        return ListT(_apply(ty.elem, subst))
    if isinstance(ty, RecordT):
        return RecordT({k: _apply(v, subst) for k, v in ty.fields.items()})
    return ty


def _occurs(var: str, ty: Type, subst: Subst) -> bool:
    ty = _apply(ty, subst)
    if isinstance(ty, Var):
        return ty.name == var
    if isinstance(ty, ListT):
        return _occurs(var, ty.elem, subst)
    if isinstance(ty, RecordT):
        return any(_occurs(var, v, subst) for v in ty.fields.values())
    return False


def _num_join(a: str, b: str) -> Optional[str]:
    if a == b:
        return a
    if {a, b} == {"Int", "Float"}:
        return "Float"
    return None


def unify(expected: Type, actual: Type, subst: Subst) -> bool:
    """Constraint: actual must be assignable to expected.

    This may bind type variables appearing in expected (or actual).
    """
    expected = _apply(expected, subst)
    actual = _apply(actual, subst)

    if isinstance(expected, AnyType):
        return True
    if isinstance(actual, AnyType):
        # unknown actual is acceptable
        return True

    if isinstance(expected, Var):
        if expected.name in subst:
            return unify(subst[expected.name], actual, subst)
        if _occurs(expected.name, actual, subst):
            return True
        subst[expected.name] = actual
        return True

    if isinstance(actual, Var):
        if actual.name in subst:
            return unify(expected, subst[actual.name], subst)
        if _occurs(actual.name, expected, subst):
            return True
        subst[actual.name] = expected
        return True

    if isinstance(expected, Prim) and isinstance(actual, Prim):
        if expected.name == actual.name:
            return True
        j = _num_join(expected.name, actual.name)
        return j is not None and j == expected.name

    if isinstance(expected, ListT) and isinstance(actual, ListT):
        return unify(expected.elem, actual.elem, subst)

    if isinstance(expected, RecordT) and isinstance(actual, RecordT):
        # structural: actual may have extra fields, must contain expected fields
        for k, texp in expected.fields.items():
            if k not in actual.fields:
                return False
            if not unify(texp, actual.fields[k], subst):
                return False
        return True

    return False


def join(t1: Type, t2: Type) -> Type:
    """Join (least upper bound) used for merging branch results."""
    if isinstance(t1, AnyType) or isinstance(t2, AnyType):
        return AnyType()
    if isinstance(t1, Prim) and isinstance(t2, Prim):
        j = _num_join(t1.name, t2.name)
        return Prim(j) if j else AnyType()
    if isinstance(t1, ListT) and isinstance(t2, ListT):
        return ListT(join(t1.elem, t2.elem))
    if isinstance(t1, RecordT) and isinstance(t2, RecordT):
        common = set(t1.fields.keys()) & set(t2.fields.keys())
        return RecordT({k: join(t1.fields[k], t2.fields[k]) for k in sorted(common)})
    if isinstance(t1, Var):
        return t2
    if isinstance(t2, Var):
        return t1
    return AnyType()


# -------------------------
# Diagnostics
# -------------------------

@dataclass
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


# -------------------------
# Signatures
# -------------------------

@dataclass(frozen=True)
class Sig:
    name: str
    type_params: List[str]
    param_names: List[str]
    param_types: List[Type]
    ret: Type


def _builtin_sigs() -> Dict[str, Sig]:
    # builtins expressed as type signatures
    # NOTE: some names (and/or/not) are keywords in Python but remain valid in Astra.
    T = Var("T")
    return {
        # arithmetic
        "add": Sig("add", [], ["a", "b"], [Prim("Int"), Prim("Int")], Prim("Int")),
        "sub": Sig("sub", [], ["a", "b"], [Prim("Int"), Prim("Int")], Prim("Int")),
        "mul": Sig("mul", [], ["a", "b"], [Prim("Int"), Prim("Int")], Prim("Int")),
        "div": Sig("div", [], ["a", "b"], [Prim("Int"), Prim("Int")], Prim("Float")),
        # comparisons
        "eq": Sig("eq", [], ["a", "b"], [AnyType(), AnyType()], Prim("Bool")),
        "neq": Sig("neq", [], ["a", "b"], [AnyType(), AnyType()], Prim("Bool")),
        "lt": Sig("lt", [], ["a", "b"], [Prim("Int"), Prim("Int")], Prim("Bool")),
        "lte": Sig("lte", [], ["a", "b"], [Prim("Int"), Prim("Int")], Prim("Bool")),
        "gt": Sig("gt", [], ["a", "b"], [Prim("Int"), Prim("Int")], Prim("Bool")),
        "gte": Sig("gte", [], ["a", "b"], [Prim("Int"), Prim("Int")], Prim("Bool")),
        # boolean
        "and": Sig("and", [], ["a", "b"], [Prim("Bool"), Prim("Bool")], Prim("Bool")),
        "or": Sig("or", [], ["a", "b"], [Prim("Bool"), Prim("Bool")], Prim("Bool")),
        "not": Sig("not", [], ["a"], [Prim("Bool")], Prim("Bool")),
        # strings
        "str_len": Sig("str_len", [], ["s"], [Prim("String")], Prim("Int")),
        "str_concat": Sig("str_concat", [], ["a", "b"], [Prim("String"), Prim("String")], Prim("String")),
        "str_contains": Sig("str_contains", [], ["s", "sub"], [Prim("String"), Prim("String")], Prim("Bool")),
        # lists
        "len": Sig("len", ["T"], ["xs"], [ListT(Var("T"))], Prim("Int")),
        "list_get": Sig("list_get", ["T"], ["xs", "i"], [ListT(Var("T")), Prim("Int")], Var("T")),
        "list_set": Sig("list_set", ["T"], ["xs", "i", "v"], [ListT(Var("T")), Prim("Int"), Var("T")], ListT(Var("T"))),
        "list_append": Sig("list_append", ["T"], ["xs", "v"], [ListT(Var("T")), Var("T")], ListT(Var("T"))),
        "list_concat": Sig("list_concat", ["T"], ["a", "b"], [ListT(Var("T")), ListT(Var("T"))], ListT(Var("T"))),
        # start/end can be Int or Null; we model them as Any for pragmatic flexibility
        "list_slice": Sig("list_slice", ["T"], ["xs", "start", "end"], [ListT(Var("T")), AnyType(), AnyType()], ListT(Var("T"))),
        "list_range": Sig("list_range", [], ["n"], [Prim("Int")], ListT(Prim("Int"))),
        # higher-order list ops (refined by special-case inference)
        "list_map": Sig("list_map", ["T"], ["fn", "xs"], [Prim("String"), ListT(Var("T"))], ListT(AnyType())),
        "list_filter": Sig("list_filter", ["T"], ["fn", "xs"], [Prim("String"), ListT(Var("T"))], ListT(Var("T"))),
        "list_reduce": Sig("list_reduce", [], ["fn", "init", "xs"], [Prim("String"), AnyType(), ListT(AnyType())], AnyType()),
        "list_sum": Sig("list_sum", [], ["xs"], [ListT(AnyType())], AnyType()),
        "list_mean": Sig("list_mean", [], ["xs"], [ListT(AnyType())], Prim("Float")),
        # objects/records (refined by special-case inference)
        "obj_get": Sig("obj_get", [], ["obj", "key"], [AnyType(), Prim("String")], AnyType()),
        "obj_get_or": Sig("obj_get_or", [], ["obj", "key", "default"], [AnyType(), Prim("String"), AnyType()], AnyType()),
        "obj_has": Sig("obj_has", [], ["obj", "key"], [AnyType(), Prim("String")], Prim("Bool")),
        "obj_set": Sig("obj_set", [], ["obj", "key", "value"], [AnyType(), Prim("String"), AnyType()], AnyType()),
        "obj_del": Sig("obj_del", [], ["obj", "key"], [AnyType(), Prim("String")], AnyType()),
        "obj_keys": Sig("obj_keys", [], ["obj"], [AnyType()], ListT(Prim("String"))),
        "obj_merge": Sig("obj_merge", [], ["a", "b"], [AnyType(), AnyType()], AnyType()),
        # effects
        "print": Sig("print", [], ["x"], [AnyType()], Prim("Null")),
        "http_get": Sig("http_get", [], ["url"], [Prim("String")], Prim("String")),
    }


def _freshen(sig: Sig, counter: List[int]) -> Tuple[Sig, Subst]:
    """Instantiate generic type params with fresh unique vars."""
    subst: Subst = {}
    for tp in sig.type_params:
        counter[0] += 1
        subst[tp] = Var(f"{tp}#{counter[0]}")

    def f(ty: Type) -> Type:
        return _apply(ty, subst)

    return Sig(sig.name, [], sig.param_names, [f(t) for t in sig.param_types], f(sig.ret)), subst


def _type_of_literal(v: Any) -> Type:
    if v is None:
        return Prim("Null")
    if isinstance(v, bool):
        return Prim("Bool")
    if isinstance(v, int) and not isinstance(v, bool):
        return Prim("Int")
    if isinstance(v, float):
        return Prim("Float")
    if isinstance(v, str):
        return Prim("String")
    return AnyType()


def _join(a: Type, b: Type) -> Type:
    a = _apply(a, {})
    b = _apply(b, {})
    if isinstance(a, AnyType) or isinstance(b, AnyType):
        return AnyType()
    if isinstance(a, Prim) and isinstance(b, Prim):
        if a.name == b.name:
            return a
        if {a.name, b.name} == {"Int", "Float"}:
            return Prim("Float")
        return AnyType()
    if isinstance(a, ListType) and isinstance(b, ListType):
        return ListT(_join(a.elem, b.elem))
    if isinstance(a, RecordType) and isinstance(b, RecordType):
        common = set(a.fields.keys()) & set(b.fields.keys())
        return RecordT({k: _join(a.fields[k], b.fields[k]) for k in sorted(common)})
    if isinstance(a, Var) and isinstance(b, Var) and a.name == b.name:
        return a
    return AnyType()


def _infer_special_call(
    fn_last: str,
    fn_full: str,
    args_expr: List[Any],
    arg_types: List[Type],
    env: Dict[str, Type],
    ptr: List[Any],
    issues: List[Issue],
    sigs: Dict[str, Sig],
    counter: List[int],
) -> Optional[Type]:
    """Special-case inference for a few stdlib calls.

    This keeps Astra's core type system small while enabling ergonomic stdlib features:
    - higher-order list ops (list_map/list_filter/list_reduce) where the first arg is
      a string literal function name
    - record field access via obj_get/obj_get_or/obj_set/obj_del when the key is a string literal
    - numeric list aggregations list_sum/list_mean
    """

    # -------------------------
    # list_sum / list_mean
    # -------------------------
    if fn_last == 'list_sum' and len(arg_types) == 1:
        xs_t = arg_types[0]
        if not isinstance(xs_t, ListT):
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 0]), 'TypeMismatch', f'list_sum expects a list, got {xs_t}'))
            return AnyType()
        elem = _apply(xs_t.elem, {})
        if isinstance(elem, Prim) and elem.name in {'Int', 'Float'}:
            return elem
        if isinstance(elem, AnyType) or isinstance(elem, Var):
            return AnyType()
        issues.append(Issue(join_pointer(ptr + ['call', 'args', 0]), 'TypeMismatch', f'list_sum expects List[Int] or List[Float], got {xs_t}'))
        return AnyType()

    if fn_last == 'list_mean' and len(arg_types) == 1:
        xs_t = arg_types[0]
        if not isinstance(xs_t, ListT):
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 0]), 'TypeMismatch', f'list_mean expects a list, got {xs_t}'))
            return Prim('Float')
        elem = _apply(xs_t.elem, {})
        if isinstance(elem, Prim) and elem.name in {'Int', 'Float'}:
            return Prim('Float')
        if isinstance(elem, AnyType) or isinstance(elem, Var):
            return Prim('Float')
        issues.append(Issue(join_pointer(ptr + ['call', 'args', 0]), 'TypeMismatch', f'list_mean expects List[Int] or List[Float], got {xs_t}'))
        return Prim('Float')

    # -------------------------
    # Higher-order list ops
    # -------------------------
    if fn_last in {'list_map', 'list_filter'} and len(arg_types) == 2:
        fn_ref = args_expr[0]
        xs_t = arg_types[1]
        if not isinstance(fn_ref, str):
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 0]), 'TypeError', f"{fn_last} expects first arg to be a string function name"))
            return ListT(AnyType())
        if not isinstance(xs_t, ListT):
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 1]), 'TypeMismatch', f"{fn_last} expects a list as second arg, got {xs_t}"))
            return ListT(AnyType())
        callee_last = _qual_last(fn_ref)
        callee_sig = sigs.get(callee_last) or sigs.get(fn_ref)
        if callee_sig is None:
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 0]), 'UnknownFunctionCall', f"Unknown function: {fn_ref}"))
            return ListT(AnyType())
        callee_inst, _ = _freshen(callee_sig, counter)
        if len(callee_inst.param_types) != 1:
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 0]), 'ArityMismatch', f"{fn_last} expects '{fn_ref}' to take 1 arg, but it takes {len(callee_inst.param_types)}"))
            return ListT(AnyType())
        subs: Subst = {}
        if not unify(callee_inst.param_types[0], xs_t.elem, subs):
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 1]), 'TypeMismatch', f"{fn_ref} expects {callee_inst.param_types[0]} but list has {xs_t.elem}"))
        ret_t = _apply(callee_inst.ret, subs)
        if fn_last == 'list_filter':
            subs2: Subst = {}
            if not unify(Prim('Bool'), ret_t, subs2):
                issues.append(Issue(join_pointer(ptr + ['call', 'args', 0]), 'TypeMismatch', f"{fn_ref} used in list_filter must return Bool, got {ret_t}"))
            return xs_t
        # list_map
        return ListT(ret_t)

    if fn_last == 'list_reduce' and len(arg_types) == 3:
        fn_ref = args_expr[0]
        init_t = arg_types[1]
        xs_t = arg_types[2]
        if not isinstance(fn_ref, str):
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 0]), 'TypeError', 'list_reduce expects first arg to be a string function name'))
            return init_t
        if not isinstance(xs_t, ListT):
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 2]), 'TypeMismatch', f'list_reduce expects a list as third arg, got {xs_t}'))
            return init_t
        callee_last = _qual_last(fn_ref)
        callee_sig = sigs.get(callee_last) or sigs.get(fn_ref)
        if callee_sig is None:
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 0]), 'UnknownFunctionCall', f'Unknown function: {fn_ref}'))
            return init_t
        callee_inst, _ = _freshen(callee_sig, counter)
        if len(callee_inst.param_types) != 2:
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 0]), 'ArityMismatch', f"list_reduce expects '{fn_ref}' to take 2 args, but it takes {len(callee_inst.param_types)}"))
            return init_t
        subs: Subst = {}
        if not unify(callee_inst.param_types[0], init_t, subs):
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 1]), 'TypeMismatch', f"{fn_ref} first param expects {callee_inst.param_types[0]} but init is {init_t}"))
        if not unify(callee_inst.param_types[1], xs_t.elem, subs):
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 2]), 'TypeMismatch', f"{fn_ref} second param expects {callee_inst.param_types[1]} but list has {xs_t.elem}"))
        ret_t = _apply(callee_inst.ret, subs)
        subs2: Subst = {}
        if not unify(init_t, ret_t, subs2):
            issues.append(Issue(join_pointer(ptr + ['call', 'args', 0]), 'TypeMismatch', f"{fn_ref} used in list_reduce must return a type compatible with init ({init_t}), got {ret_t}"))
        return init_t

    # -------------------------
    # Record helpers via obj_* when key is a string literal
    # -------------------------
    if fn_last in {'obj_get', 'obj_get_or', 'obj_set', 'obj_del', 'obj_merge'}:
        # only kick in when the key is a literal string and we have record types
        if fn_last in {'obj_get', 'obj_del'} and len(arg_types) == 2:
            obj_t = arg_types[0]
            key = args_expr[1]
            if isinstance(obj_t, RecordT) and isinstance(key, str):
                if fn_last == 'obj_get':
                    if key in obj_t.fields:
                        return obj_t.fields[key]
                    issues.append(Issue(join_pointer(ptr + ['call', 'args', 1]), 'UnknownField', f"Record has no field '{key}'"))
                    return AnyType()
                # obj_del
                new_fields = dict(obj_t.fields)
                new_fields.pop(key, None)
                return RecordT(new_fields)
            return None

        if fn_last == 'obj_get_or' and len(arg_types) == 3:
            obj_t = arg_types[0]
            key = args_expr[1]
            default_t = arg_types[2]
            if isinstance(obj_t, RecordT) and isinstance(key, str):
                if key in obj_t.fields:
                    return _join(obj_t.fields[key], default_t)
                issues.append(Issue(join_pointer(ptr + ['call', 'args', 1]), 'UnknownField', f"Record has no field '{key}'"))
                return _join(AnyType(), default_t)
            return None

        if fn_last == 'obj_set' and len(arg_types) == 3:
            obj_t = arg_types[0]
            key = args_expr[1]
            val_t = arg_types[2]
            if isinstance(obj_t, RecordT) and isinstance(key, str):
                new_fields = dict(obj_t.fields)
                if key in new_fields:
                    new_fields[key] = _join(new_fields[key], val_t)
                else:
                    new_fields[key] = val_t
                return RecordT(new_fields)
            return None

        if fn_last == 'obj_merge' and len(arg_types) == 2:
            a_t = arg_types[0]
            b_t = arg_types[1]
            if isinstance(a_t, RecordT) and isinstance(b_t, RecordT):
                merged = dict(a_t.fields)
                for k, v in b_t.fields.items():
                    if k in merged:
                        merged[k] = _join(merged[k], v)
                    else:
                        merged[k] = v
                return RecordT(merged)
            return None

    return None

def _infer_expr(expr: Any, env: Dict[str, Type], ptr: List[Any], issues: List[Issue], sigs: Dict[str, Sig], counter: List[int]) -> Type:
    # literals
    if isinstance(expr, (int, float, str, bool)) or expr is None:
        return _type_of_literal(expr)

    if not isinstance(expr, dict):
        issues.append(Issue(join_pointer(ptr), "TypeError", "Expression must be literal or object"))
        return AnyType()

    if "var" in expr:
        name = expr.get("var")
        if not isinstance(name, str):
            issues.append(Issue(join_pointer(ptr + ["var"]), "TypeError", "var must be a string"))
            return AnyType()
        if name not in env:
            issues.append(Issue(join_pointer(ptr + ["var"]), "UndefinedVariable", f"Undefined variable: {name}"))
            return AnyType()
        return env[name]

    if "list" in expr:
        arr = expr.get("list")
        if not isinstance(arr, list):
            issues.append(Issue(join_pointer(ptr + ["list"]), "TypeError", "list must be an array"))
            return AnyType()
        if not arr:
            return ListT(AnyType())
        t = _infer_expr(arr[0], env, ptr + ["list", 0], issues, sigs, counter)
        for i in range(1, len(arr)):
            ti = _infer_expr(arr[i], env, ptr + ["list", i], issues, sigs, counter)
            t = _join(t, ti)
        return ListT(t)

    if "obj" in expr:
        obj = expr.get("obj")
        if not isinstance(obj, dict):
            issues.append(Issue(join_pointer(ptr + ["obj"]), "TypeError", "obj must be an object"))
            return AnyType()
        fields: Dict[str, Type] = {}
        for k, v in obj.items():
            fields[k] = _infer_expr(v, env, ptr + ["obj", k], issues, sigs, counter)
        return RecordT(fields)

    if "call" in expr:
        call = expr.get("call")
        if not isinstance(call, dict):
            issues.append(Issue(join_pointer(ptr + ["call"]), "TypeError", "call must be an object"))
            return AnyType()
        fn = call.get("fn")
        args = call.get("args", [])
        if not isinstance(fn, str):
            issues.append(Issue(join_pointer(ptr + ["call", "fn"]), "TypeError", "call.fn must be a string"))
            return AnyType()
        if not isinstance(args, list):
            issues.append(Issue(join_pointer(ptr + ["call", "args"]), "TypeError", "call.args must be an array"))
            return AnyType()

        # Infer arg types first
        arg_types: List[Type] = []
        for i, a in enumerate(args):
            arg_types.append(_infer_expr(a, env, ptr + ["call", "args", i], issues, sigs, counter))
        fn_last = _qual_last(fn)

        special = _infer_special_call(fn_last, fn, args, arg_types, env, ptr, issues, sigs, counter)
        if special is not None:
            return special

        sig = sigs.get(fn_last) or sigs.get(fn)
        if sig is None:
            issues.append(Issue(join_pointer(ptr + ["call", "fn"]), "UnknownFunctionCall", f"Unknown function: {fn}"))
            return AnyType()

        inst, _ = _freshen(sig, counter)
        if len(arg_types) != len(inst.param_types):
            issues.append(Issue(join_pointer(ptr + ["call"]), "ArityMismatch", f"{fn} expects {len(inst.param_types)} args but got {len(arg_types)}"))
            return AnyType()

        subs: Subst = {}
        # unify params
        for i, (expected, actual) in enumerate(zip(inst.param_types, arg_types)):
            ok = unify(expected, actual, subs)
            if not ok:
                issues.append(Issue(join_pointer(ptr + ["call", "args", i]), "TypeMismatch", f"Arg {i} to {fn} expected {expected} but got {actual}"))

        return _apply(inst.ret, subs)

    issues.append(Issue(join_pointer(ptr), "TypeError", f"Unknown expr form: {list(expr.keys())}"))
    return AnyType()


def _check_stmt(stmt: Any, env: Dict[str, Type], ptr: List[Any], issues: List[Issue], sigs: Dict[str, Sig], counter: List[int], ret_ann: Type, ret_seen: List[Type]) -> Tuple[Dict[str, Type], bool]:
    """Return (new_env, always_returns)."""
    if not isinstance(stmt, dict) or len(stmt.keys()) != 1:
        issues.append(Issue(join_pointer(ptr), "TypeError", "Statement must be an object with exactly one key"))
        return env, False

    tag = next(iter(stmt.keys()))
    val = stmt[tag]

    if tag == "let":
        if not isinstance(val, dict):
            issues.append(Issue(join_pointer(ptr + ["let"]), "TypeError", "let must be an object"))
            return env, False
        name = val.get("name")
        if not isinstance(name, str):
            issues.append(Issue(join_pointer(ptr + ["let", "name"]), "TypeError", "let.name must be a string"))
            return env, False
        expr = val.get("expr")
        t = _infer_expr(expr, env, ptr + ["let", "expr"], issues, sigs, counter)
        if name in env:
            # semantic checker already flags; keep as type error too
            issues.append(Issue(join_pointer(ptr + ["let", "name"]), "Rebind", f"Variable '{name}' is already defined"))
        new_env = dict(env)
        new_env[name] = t
        return new_env, False

    if tag == "assert":
        if not isinstance(val, dict):
            issues.append(Issue(join_pointer(ptr + ["assert"]), "TypeError", "assert must be an object"))
            return env, False
        e = val.get("expr")
        t = _infer_expr(e, env, ptr + ["assert", "expr"], issues, sigs, counter)
        subs: Subst = {}
        if not unify(Prim("Bool"), t, subs):
            issues.append(Issue(join_pointer(ptr + ["assert", "expr"]), "TypeMismatch", f"assert expr must be Bool, got {t}"))
        return env, False

    if tag == "expr":
        _infer_expr(val, env, ptr + ["expr"], issues, sigs, counter)
        return env, False

    if tag == "return":
        t = _infer_expr(val, env, ptr + ["return"], issues, sigs, counter)
        # check return annotation
        subs: Subst = {}
        if not unify(ret_ann, t, subs):
            issues.append(Issue(join_pointer(ptr + ["return"]), "ReturnTypeMismatch", f"Return expected {ret_ann} but got {t}"))
        ret_seen.append(_apply(t, subs))
        return env, True

    if tag == "if":
        if not isinstance(val, dict):
            issues.append(Issue(join_pointer(ptr + ["if"]), "TypeError", "if must be an object"))
            return env, False
        cond = val.get("cond")
        tcond = _infer_expr(cond, env, ptr + ["if", "cond"], issues, sigs, counter)
        subs: Subst = {}
        if not unify(Prim("Bool"), tcond, subs):
            issues.append(Issue(join_pointer(ptr + ["if", "cond"]), "TypeMismatch", f"if.cond must be Bool, got {tcond}"))

        then = val.get("then", [])
        els = val.get("else", [])
        if not isinstance(then, list) or not isinstance(els, list):
            issues.append(Issue(join_pointer(ptr + ["if"]), "TypeError", "if.then and if.else must be arrays"))
            return env, False

        env_then, ret_then = _check_block(then, dict(env), ptr + ["if", "then"], issues, sigs, counter, ret_ann, ret_seen)
        env_else, ret_else = _check_block(els, dict(env), ptr + ["if", "else"], issues, sigs, counter, ret_ann, ret_seen)

        # merge env: keep vars defined in both, join types
        merged: Dict[str, Type] = {}
        for k in set(env_then.keys()) & set(env_else.keys()):
            merged[k] = _join(env_then[k], env_else[k])
        # also keep vars that existed before in outer env
        for k in env.keys():
            merged[k] = merged.get(k, env[k])

        return merged, (ret_then and ret_else)

    issues.append(Issue(join_pointer(ptr), "TypeError", f"Unknown stmt: {tag}"))
    return env, False


def _check_block(stmts: List[Any], env: Dict[str, Type], ptr: List[Any], issues: List[Issue], sigs: Dict[str, Sig], counter: List[int], ret_ann: Type, ret_seen: List[Type]) -> Tuple[Dict[str, Type], bool]:
    always_returns = False
    cur_env = env
    for i, s in enumerate(stmts):
        if always_returns:
            # still scan for type errors inside? keep noise low; skip.
            continue
        cur_env, ar = _check_stmt(s, cur_env, ptr + [i], issues, sigs, counter, ret_ann, ret_seen)
        if ar:
            always_returns = True
    return cur_env, always_returns


def _sig_from_function(fn: Dict[str, Any]) -> Sig:
    name = fn.get("name")
    params = fn.get("params", []) or []
    if not isinstance(name, str):
        name = "<anon>"
    if not isinstance(params, list):
        params = []
    param_names = [p if isinstance(p, str) else "_" for p in params]

    type_params = fn.get("type_params", []) or []
    if not isinstance(type_params, list):
        type_params = []
    type_params = [tp for tp in type_params if isinstance(tp, str)]

    # param_types optional
    pt_raw = fn.get("param_types")
    if isinstance(pt_raw, list) and len(pt_raw) == len(param_names):
        param_types = [parse_type_expr(t) for t in pt_raw]
    else:
        param_types = [AnyType() for _ in param_names]

    ret_raw = fn.get("returns")
    ret = parse_type_expr(ret_raw) if isinstance(ret_raw, str) else AnyType()

    return Sig(name, type_params, param_names, param_types, ret)


def check_module(module: Dict[str, Any]) -> List[Dict[str, Any]]:
    issues: List[Issue] = []

    # build signatures
    sigs: Dict[str, Sig] = _builtin_sigs()
    for fn in module.get("functions", []) or []:
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            sigs[fn["name"]] = _sig_from_function(fn)

    counter = [0]  # for fresh vars

    # typecheck each function
    for fi, fn in enumerate(module.get("functions", []) or []):
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str):
            continue
        sig = sigs[name]
        # init env with params
        env: Dict[str, Type] = {}
        for p, t in zip(sig.param_names, sig.param_types):
            env[p] = t

        ret_seen: List[Type] = []

        # requires/ensures
        for ri, req in enumerate(fn.get("requires", []) or []):
            t = _infer_expr(req, env, ["functions", fi, "requires", ri], issues, sigs, counter)
            subs: Subst = {}
            if not unify(Prim("Bool"), t, subs):
                issues.append(Issue(join_pointer(["functions", fi, "requires", ri]), "TypeMismatch", f"requires must be Bool, got {t}"))

        # body
        body = fn.get("body", []) or []
        if isinstance(body, list):
            _check_block(body, env, ["functions", fi, "body"], issues, sigs, counter, sig.ret, ret_seen)

        # missing return: if declared return not Null/Any and no return seen
        if not ret_seen and not isinstance(sig.ret, AnyType) and not (isinstance(sig.ret, Prim) and sig.ret.name == "Null"):
            issues.append(Issue(join_pointer(["functions", fi]), "MissingReturn", f"Function '{name}' may fall through without returning"))

        # ensures: env includes result
        env_post = dict(env)
        env_post["result"] = sig.ret
        for ei, ens in enumerate(fn.get("ensures", []) or []):
            t = _infer_expr(ens, env_post, ["functions", fi, "ensures", ei], issues, sigs, counter)
            subs: Subst = {}
            if not unify(Prim("Bool"), t, subs):
                issues.append(Issue(join_pointer(["functions", fi, "ensures", ei]), "TypeMismatch", f"ensures must be Bool, got {t}"))

        # function-level tests
        for ti, tc in enumerate(fn.get("tests", []) or []):
            if not isinstance(tc, dict):
                continue
            args = tc.get("args", []) or []
            if not isinstance(args, list):
                continue
            arg_types = [_infer_expr(a, env, ["functions", fi, "tests", ti, "args", ai], issues, sigs, counter) for ai, a in enumerate(args)]
            inst, _ = _freshen(sig, counter)
            if len(arg_types) != len(inst.param_types):
                issues.append(Issue(join_pointer(["functions", fi, "tests", ti]), "TestArityMismatch", f"Test for {name} has wrong arity"))
            else:
                subs: Subst = {}
                for ai, (e, a) in enumerate(zip(inst.param_types, arg_types)):
                    if not unify(e, a, subs):
                        issues.append(Issue(join_pointer(["functions", fi, "tests", ti, "args", ai]), "TypeMismatch", f"Test arg expected {e} got {a}"))
                exp = tc.get("expect")
                exp_t = _infer_expr(exp, env, ["functions", fi, "tests", ti, "expect"], issues, sigs, counter)
                if not unify(_apply(inst.ret, subs), exp_t, subs):
                    issues.append(Issue(join_pointer(["functions", fi, "tests", ti, "expect"]), "TypeMismatch", f"Expected {inst.ret} got {exp_t}"))

    # module-level tests
    for ti, tc in enumerate(module.get("tests", []) or []):
        if not isinstance(tc, dict):
            continue
        fn_name = tc.get("fn")
        if not isinstance(fn_name, str):
            continue
        if fn_name not in sigs:
            issues.append(Issue(join_pointer(["tests", ti, "fn"]), "UnknownFunctionCall", f"Unknown function: {fn_name}"))
            continue
        sig = sigs[fn_name]
        args = tc.get("args", []) or []
        if not isinstance(args, list):
            continue
        env: Dict[str, Type] = {}
        arg_types = [_infer_expr(a, env, ["tests", ti, "args", ai], issues, sigs, counter) for ai, a in enumerate(args)]
        inst, _ = _freshen(sig, counter)
        if len(arg_types) != len(inst.param_types):
            issues.append(Issue(join_pointer(["tests", ti]), "TestArityMismatch", f"Test for {fn_name} has wrong arity"))
        else:
            subs: Subst = {}
            for ai, (e, a) in enumerate(zip(inst.param_types, arg_types)):
                if not unify(e, a, subs):
                    issues.append(Issue(join_pointer(["tests", ti, "args", ai]), "TypeMismatch", f"Test arg expected {e} got {a}"))
            exp_t = _infer_expr(tc.get("expect"), env, ["tests", ti, "expect"], issues, sigs, counter)
            if not unify(_apply(inst.ret, subs), exp_t, subs):
                issues.append(Issue(join_pointer(["tests", ti, "expect"]), "TypeMismatch", f"Expected {inst.ret} got {exp_t}"))

    return [i.to_dict() for i in issues]


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="astra typecheck")
    ap.add_argument("path", help="Path to Astra module JSON")
    ap.add_argument("--json", action="store_true", help="Emit issues as JSON")
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
            print(f"{i['code']} {i['pointer']}: {i['message']}")

    has_errors = any(i.get("severity", "error") == "error" for i in issues)
    return 2 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
