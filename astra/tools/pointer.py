"""JSON Pointer + JSON Patch utilities.

We use JSON Pointer (RFC 6901) in diagnostics to indicate exact AST locations.
We use a minimal JSON Patch (RFC 6902) subset for automated repairs.

Supported patch operations:
- add
- replace
- remove

Notes:
- For lists, the special index '-' is supported (append).
- For 'add' into a list at index i, we insert.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Union

Json = Union[None, bool, int, float, str, List["Json"], Dict[str, "Json"]]


def escape_segment(seg: str) -> str:
    return seg.replace("~", "~0").replace("/", "~1")


def unescape_segment(seg: str) -> str:
    return seg.replace("~1", "/").replace("~0", "~")


def split_pointer(pointer: str) -> List[str]:
    if pointer in ("", "/"):
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"Invalid JSON pointer (must start with '/'): {pointer}")
    return [unescape_segment(p) for p in pointer.lstrip("/").split("/")]


def join_pointer(segments: List[Union[str, int]]) -> str:
    if not segments:
        return ""
    out: List[str] = []
    for s in segments:
        out.append(escape_segment(str(s)))
    return "/" + "/".join(out)


def _coerce_index(seg: str, cur: Any) -> Union[str, int]:
    """Interpret seg as int index only if current container is a list."""
    if isinstance(cur, list) and seg != "-" and seg.isdigit():
        return int(seg)
    return seg


def resolve(doc: Json, pointer: str) -> Json:
    cur: Any = doc
    for raw in split_pointer(pointer):
        seg = _coerce_index(raw, cur)
        if isinstance(cur, list):
            if seg == "-":
                raise KeyError("'-' is not valid for resolve")
            if not isinstance(seg, int):
                raise KeyError(f"Expected list index, got {seg!r}")
            cur = cur[seg]
        elif isinstance(cur, dict):
            if not isinstance(seg, str):
                raise KeyError(f"Expected object key, got {seg!r}")
            cur = cur[seg]
        else:
            raise KeyError(f"Cannot traverse into non-container at segment {raw!r}")
    return cur


def _resolve_parent(doc: Json, pointer: str) -> Tuple[Any, Union[str, int]]:
    segs = split_pointer(pointer)
    if not segs:
        raise ValueError("Pointer refers to document root; no parent")
    parent_ptr = join_pointer(segs[:-1])
    parent: Any = resolve(doc, parent_ptr)
    last_raw = segs[-1]
    last = _coerce_index(last_raw, parent)
    return parent, last


@dataclass
class PatchError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


def apply_patch(doc: Json, patch: List[Dict[str, Any]]) -> Json:
    """Apply a minimal subset of JSON Patch (RFC 6902) to doc."""
    for op in patch:
        if not isinstance(op, dict):
            raise PatchError("Patch operation must be an object")
        kind = op.get("op")
        path = op.get("path")
        if kind not in ("add", "replace", "remove"):
            raise PatchError(f"Unsupported patch op: {kind}")
        if not isinstance(path, str):
            raise PatchError("Patch op missing string 'path'")

        if kind == "remove":
            parent, key = _resolve_parent(doc, path)
            if isinstance(parent, list):
                if key == "-" or not isinstance(key, int):
                    raise PatchError("Invalid list index for remove")
                parent.pop(key)
            elif isinstance(parent, dict):
                if not isinstance(key, str):
                    raise PatchError("Invalid object key for remove")
                parent.pop(key, None)
            else:
                raise PatchError("Parent is not container")
            continue

        # add / replace
        if "value" not in op:
            raise PatchError(f"Patch op '{kind}' missing 'value'")
        value = op["value"]

        if path in ("", "/"):
            # replace root
            if kind == "add" or kind == "replace":
                doc = value
            continue

        parent, key = _resolve_parent(doc, path)
        if isinstance(parent, list):
            if key == "-":
                # append
                parent.append(value)
            else:
                if not isinstance(key, int):
                    raise PatchError("Invalid list index")
                if kind == "add":
                    parent.insert(key, value)
                else:
                    parent[key] = value
        elif isinstance(parent, dict):
            if not isinstance(key, str):
                raise PatchError("Invalid object key")
            parent[key] = value
        else:
            raise PatchError("Parent is not container")

    return doc
