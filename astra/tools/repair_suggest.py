"""Deterministic repair suggestions (no LLM).

Given:
- the current module AST
- a list of issues emitted by checkers

This module proposes a small set of safe, mechanical JSON Patch operations.

Intended uses:
- LSP quick fixes
- as a fallback inside the repair loop before calling an LLM

It is deliberately conservative.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from astra.tools.pointer import apply_patch
from astra.tools import effects


def suggest_patches(module: Dict[str, Any], issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    patches: List[Dict[str, Any]] = []

    # Precompute transitive effects only if needed.
    need_effects_map = any(it.get("code") == "MissingEffect" for it in issues)
    effects_map: Dict[str, Any] = {}
    if need_effects_map:
        try:
            effects_map, _ = effects.compute_transitive_effects(module)
        except Exception:
            effects_map = {}

    # MissingReturn -> append return null
    for it in issues:
        if it.get("code") == "MissingReturn":
            ptr = it.get("pointer", "")
            # pointer like /functions/3
            parts = [p for p in ptr.strip("/").split("/") if p]
            if len(parts) >= 2 and parts[0] == "functions" and parts[1].isdigit():
                fi = int(parts[1])
                patches.append(
                    {
                        "op": "add",
                        "path": f"/functions/{fi}/body/-",
                        "value": {"return": None},
                    }
                )

    # NotPure -> remove 'pure' from declared effects (minimal remove patch)
    for it in issues:
        if it.get("code") == "NotPure":
            ptr = it.get("pointer", "")
            parts = [p for p in ptr.strip("/").split("/") if p]
            if len(parts) >= 3 and parts[0] == "functions" and parts[1].isdigit() and parts[2] == "effects":
                fi = int(parts[1])
                fn = (module.get("functions", []) or [])[fi]
                eff = fn.get("effects", []) if isinstance(fn, dict) else []
                if isinstance(eff, list) and "pure" in eff and len(eff) > 1:
                    idx = eff.index("pure")
                    patches.append({"op": "remove", "path": f"/functions/{fi}/effects/{idx}"})

    # MissingEffect -> add required missing effects by replacing the list (single patch)
    for it in issues:
        if it.get("code") == "MissingEffect":
            ptr = it.get("pointer", "")
            parts = [p for p in ptr.strip("/").split("/") if p]
            if len(parts) >= 3 and parts[0] == "functions" and parts[1].isdigit() and parts[2] == "effects":
                fi = int(parts[1])
                fn = (module.get("functions", []) or [])[fi]
                if not isinstance(fn, dict):
                    continue
                name = fn.get("name")
                if not isinstance(name, str):
                    continue
                required = set(effects_map.get(name, set()) or set())
                declared_list = fn.get("effects", [])
                if not isinstance(declared_list, list):
                    declared_list = []
                declared = list(declared_list)
                if not declared:
                    declared = ["pure"]
                missing = required - set(declared)
                if not missing:
                    continue
                new_eff = list(declared)
                for m in sorted(missing):
                    if m not in new_eff:
                        new_eff.append(m)

                # If adding any non-pure effects, drop 'pure' to avoid NotPure warnings.
                if "pure" in new_eff and len(new_eff) > 1:
                    new_eff = [e for e in new_eff if e != "pure"]
                patches.append({"op": "replace", "path": f"/functions/{fi}/effects", "value": new_eff})

    # De-duplicate patches (simple)
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for p in patches:
        key = json.dumps(p, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            uniq.append(p)
            seen.add(key)

    return uniq


def apply_suggestions(module: Dict[str, Any], issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    patches = suggest_patches(module, issues)
    if not patches:
        return module
    return apply_patch(module, patches)
