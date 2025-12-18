"""Schema validation + canonical formatter for Astra modules (v1.0).

Why:
- LLM outputs should be structurally valid and easy to repair.
- Canonical formatting makes diffs stable and reduces churn.
- Validation errors are emitted with JSON Pointers.

CLI:
  astra format module.json [--in-place] [--check] [--json-errors]

Exit codes:
  0 OK
  1 --check diff
  2 schema validation failed
  3 IO/JSON parse error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import jsonschema

from astra.tools.pointer import join_pointer

try:
    # Python 3.9+
    import importlib.resources as importlib_resources
except Exception:  # pragma: no cover
    import importlib_resources  # type: ignore


def load_schema_text() -> str:
    with importlib_resources.files("astra.schema").joinpath("astra.schema.v1.json").open("r", encoding="utf-8") as f:
        return f.read()


def load_schema() -> Dict[str, Any]:
    return json.loads(load_schema_text())


def load_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def validate(ast: Any, schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    validator = jsonschema.Draft202012Validator(schema)
    errors: List[Dict[str, Any]] = []
    for err in sorted(validator.iter_errors(ast), key=lambda e: list(e.absolute_path)):
        pointer = join_pointer(list(err.absolute_path))
        errors.append(
            {
                "pointer": pointer,
                "message": err.message,
                "validator": err.validator,
                "expected": err.validator_value,
            }
        )
    return errors


# ---- Canonicalization (stable ordering) ----

MODULE_KEY_ORDER = [
    "module",
    "version",
    "imports",
    "externs",
    "functions",
    "tests",
    "properties",
    "metadata",
]

FUNCTION_KEY_ORDER = [
    "name",
    "doc",
    "type_params",
    "params",
    "param_types",
    "returns",
    "effects",
    "requires",
    "ensures",
    "body",
    "tests",
    "properties",
]

TEST_KEY_ORDER = ["name", "fn", "args", "expect"]

PROPERTY_KEY_ORDER = ["name", "fn", "strategy", "expect"]

# statement wrappers always single-key
IF_KEY_ORDER = ["cond", "then", "else"]
LET_KEY_ORDER = ["name", "expr"]
ASSERT_KEY_ORDER = ["expr", "message"]
CALL_KEY_ORDER = ["fn", "args"]


def _ordered_dict(obj: Dict[str, Any], preferred: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in preferred:
        if k in obj:
            out[k] = obj[k]
    for k in sorted([k for k in obj.keys() if k not in out]):
        out[k] = obj[k]
    return out


def canonicalize(node: Any) -> Any:
    if isinstance(node, list):
        return [canonicalize(x) for x in node]

    if isinstance(node, dict):
        keys = list(node.keys())

        # module root heuristic
        if "module" in node and "functions" in node:
            obj = _ordered_dict(node, MODULE_KEY_ORDER)
            return {k: canonicalize(v) for k, v in obj.items()}

        # function heuristic
        if set(["name", "params", "effects", "body"]).issubset(node.keys()):
            obj = _ordered_dict(node, FUNCTION_KEY_ORDER)
            return {k: canonicalize(v) for k, v in obj.items() if v is not None}

        # test case heuristic
        if "fn" in node and "args" in node and "expect" in node and "strategy" not in node:
            obj = _ordered_dict(node, TEST_KEY_ORDER)
            return {k: canonicalize(v) for k, v in obj.items() if v is not None}

        # property test heuristic
        if "fn" in node and "strategy" in node and "expect" in node:
            obj = _ordered_dict(node, PROPERTY_KEY_ORDER)
            return {k: canonicalize(v) for k, v in obj.items() if v is not None}

        # statement wrappers
        if len(keys) == 1 and keys[0] in ("if", "let", "return", "assert", "expr"):
            tag = keys[0]
            val = node[tag]
            if tag == "if" and isinstance(val, dict):
                inner = _ordered_dict(val, IF_KEY_ORDER)
                return {"if": {k: canonicalize(v) for k, v in inner.items() if v is not None}}
            if tag == "let" and isinstance(val, dict):
                inner = _ordered_dict(val, LET_KEY_ORDER)
                return {"let": {k: canonicalize(v) for k, v in inner.items() if v is not None}}
            if tag == "assert" and isinstance(val, dict):
                inner = _ordered_dict(val, ASSERT_KEY_ORDER)
                return {"assert": {k: canonicalize(v) for k, v in inner.items() if v is not None}}
            return {tag: canonicalize(val)}

        # expression wrappers
        if len(keys) == 1 and keys[0] in ("call", "var", "list", "obj"):
            tag = keys[0]
            val = node[tag]
            if tag == "call" and isinstance(val, dict):
                inner = _ordered_dict(val, CALL_KEY_ORDER)
                return {"call": {k: canonicalize(v) for k, v in inner.items() if v is not None}}
            if tag == "obj" and isinstance(val, dict):
                return {"obj": {k: canonicalize(val[k]) for k in sorted(val.keys())}}
            return {tag: canonicalize(val)}

        # fallback: alphabetical
        obj = _ordered_dict(node, [])
        return {k: canonicalize(v) for k, v in obj.items()}

    return node


def dumps_canonical(ast: Any) -> str:
    canon = canonicalize(ast)
    return json.dumps(canon, indent=2, ensure_ascii=False) + "\n"


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="astra format")
    ap.add_argument("path", help="Path to Astra module JSON")
    ap.add_argument("--no-validate", action="store_true", help="Skip schema validation")
    ap.add_argument("--in-place", action="store_true", help="Overwrite input file")
    ap.add_argument("--out", help="Write formatted JSON to this file")
    ap.add_argument("--check", action="store_true", help="Exit 1 if formatting differs")
    ap.add_argument("--json-errors", action="store_true", help="Emit validation errors as JSON on stderr")
    args = ap.parse_args(argv)

    path = Path(args.path)
    try:
        ast = load_json(path)
    except Exception as e:
        print(f"Failed to read/parse JSON: {e}", file=sys.stderr)
        return 3

    if not args.no_validate:
        schema = load_schema()
        errors = validate(ast, schema)
        if errors:
            if args.json_errors:
                print(json.dumps(errors, indent=2, ensure_ascii=False), file=sys.stderr)
            else:
                for e in errors:
                    print(f"{e['pointer']}: {e['message']}", file=sys.stderr)
            return 2

    formatted = dumps_canonical(ast)

    if args.check:
        try:
            original = path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"Failed to read file for --check: {e}", file=sys.stderr)
            return 3
        if original != formatted:
            return 1

    if args.in_place:
        path.write_text(formatted, encoding="utf-8")
        return 0

    if args.out:
        Path(args.out).write_text(formatted, encoding="utf-8")
        return 0

    sys.stdout.write(formatted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
