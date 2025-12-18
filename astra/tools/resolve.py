"""Module resolver for Astra.

Resolves:
- imports: other Astra modules (JSON)
- externs: interface/stub modules (JSON)

Resolution strategy (v1.0):
- Merge imported/extern functions into the current module by **name**.
- If a name conflict occurs, we raise an error (deterministic).

This is intentionally simple and works well with LLM repair loops.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class ResolveError(RuntimeError):
    pass


def merge_functions(dst: Dict[str, Any], src: Dict[str, Any], *, src_label: str) -> None:
    dst_funcs = dst.setdefault("functions", [])
    if not isinstance(dst_funcs, list):
        raise ResolveError("module.functions must be a list")

    existing = {f.get("name") for f in dst_funcs if isinstance(f, dict)}
    for f in src.get("functions", []) or []:
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        if not isinstance(name, str):
            continue
        if name in existing:
            raise ResolveError(f"Function name conflict while merging {src_label}: {name}")
        dst_funcs.append(f)
        existing.add(name)


def resolve_module(module_path: Path) -> Dict[str, Any]:
    base_dir = module_path.parent
    module = load_json(module_path)

    # imports
    imports = module.get("imports", []) or []
    if not isinstance(imports, list):
        raise ResolveError("imports must be an array")
    for rel in imports:
        if not isinstance(rel, str):
            raise ResolveError("imports items must be strings")
        imp_path = (base_dir / rel).resolve()
        imported = load_json(imp_path)
        merge_functions(module, imported, src_label=f"import:{rel}")

    # externs
    externs = module.get("externs", []) or []
    if not isinstance(externs, list):
        raise ResolveError("externs must be an array")
    for rel in externs:
        if not isinstance(rel, str):
            raise ResolveError("externs items must be strings")
        ext_path = (base_dir / rel).resolve()
        extern_mod = load_json(ext_path)
        merge_functions(module, extern_mod, src_label=f"extern:{rel}")

    # After resolution, keep imports/externs for provenance OR drop them.
    # For tooling, we keep them by default, but you can strip in codegen.
    return module


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="astra resolve")
    ap.add_argument("path", help="Path to Astra module JSON")
    ap.add_argument("--out", help="Write resolved module to this file (default: stdout)")
    args = ap.parse_args(argv)

    try:
        resolved = resolve_module(Path(args.path))
    except Exception as e:
        print(f"resolve error: {e}", file=sys.stderr)
        return 2

    text = json.dumps(resolved, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
