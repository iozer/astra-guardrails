"""Astra Language Server Protocol (LSP) server (stdio).

This is a dependency-free LSP implementation intended for Astra JSON modules.

Supported features:
- diagnostics (schema + semantic + type + effect)
- completion (builtin + module function names)
- formatting (canonical JSON)
- code actions (apply canonical format / apply deterministic quick fixes)

This server is intentionally small. It speaks JSON-RPC 2.0 over stdio with
Content-Length framing.

Usage:
  astra lsp

Editor integration:
- VS Code: use a generic LSP client extension (or a small wrapper) pointing to `astra lsp`.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Set

from astra.tools import fmt, semantic, typecheck, effects
from astra.tools import jsonpos
from astra.tools.repair_suggest import suggest_patches
from astra.tools.pointer import apply_patch, resolve as ptr_resolve, split_pointer, join_pointer
from astra.tools import runtime_guarded as rt


Json = Dict[str, Any]


def _read_exact(n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sys.stdin.buffer.read(n - len(data))
        if not chunk:
            break
        data += chunk
    return data


def read_message() -> Optional[Json]:
    # Read headers
    headers: Dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.decode("utf-8", errors="replace").strip()
        if line == "":
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    if "content-length" not in headers:
        return None
    length = int(headers["content-length"])
    body = _read_exact(length)
    if not body:
        return None
    return json.loads(body.decode("utf-8", errors="replace"))


def send_message(msg: Json) -> None:
    raw = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def _full_range(text: str) -> Dict[str, Any]:
    lines = text.splitlines() or [""]
    end_line = len(lines) - 1
    # LSP expects UTF-16 code units for the character offset.
    end_char = len(lines[-1].encode("utf-16-le")) // 2
    return {"start": {"line": 0, "character": 0}, "end": {"line": end_line, "character": end_char}}


def _severity(sev: str) -> int:
    # LSP: 1=Error, 2=Warning, 3=Information, 4=Hint
    if sev == "warning":
        return 2
    if sev == "info":
        return 3
    return 1


def _pos_lt(a: Dict[str, int], b: Dict[str, int]) -> bool:
    """Return True if position a < b (line/character)."""
    if a.get("line", 0) != b.get("line", 0):
        return a.get("line", 0) < b.get("line", 0)
    return a.get("character", 0) < b.get("character", 0)


def _range_intersects(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Return True if two LSP ranges intersect (best-effort)."""
    try:
        a0, a1 = a["start"], a["end"]
        b0, b1 = b["start"], b["end"]
        # Intersection for half-open intervals: [start, end)
        return _pos_lt(a0, b1) and _pos_lt(b0, a1)
    except Exception:
        return True


# -----------------
# Minimal text edits (JSON Patch -> LSP TextEdits)
# -----------------


def _line_prefix(text: str, index: int) -> str:
    """Return the whitespace prefix on the line up to *index* (best-effort)."""
    ls = text.rfind("\n", 0, index) + 1
    prefix = text[ls:index]
    # keep only whitespace
    i = 0
    while i < len(prefix) and prefix[i] in " \t":
        i += 1
    return prefix[:i]


def _indent_after_first_line(s: str, indent: str) -> str:
    """Indent all lines *after* the first by `indent`."""
    if "\n" not in s:
        return s
    lines = s.split("\n")
    return lines[0] + "\n" + "\n".join(indent + ln for ln in lines[1:])


def _indent_all_lines(s: str, indent: str) -> str:
    """Indent all lines (including the first) by `indent`."""
    if not s:
        return s
    lines = s.split("\n")
    return "\n".join(indent + ln for ln in lines)


def _pos_from_index(doc: "Document", index: int) -> Dict[str, int]:
    if doc.index is None:
        doc.index = jsonpos.TextIndex(doc.text)
    return doc.index.position(index)


def _edit_insert(doc: "Document", index: int, new_text: str) -> Dict[str, Any]:
    pos = _pos_from_index(doc, index)
    return {"range": {"start": pos, "end": pos}, "newText": new_text}


def _edit_replace_span(doc: "Document", span: Tuple[int, int], new_text: str) -> Dict[str, Any]:
    if doc.index is None:
        doc.index = jsonpos.TextIndex(doc.text)
    rng = doc.index.range(span)
    return {"range": rng, "newText": new_text}


def _apply_text_edits_in_memory(text: str, edits: List[Dict[str, Any]]) -> str:
    """Apply LSP TextEdits to a document string and return the new text.

    This is used to *pre-validate* code actions: we simulate the edit, then
    re-parse + schema validate the result before offering the action.

    Notes:
    - LSP positions use UTF-16 code units for the character offset.
    - We apply edits in descending order of start offset to avoid offset shifts.
    - If edits overlap, we raise ValueError.
    """

    idx = jsonpos.TextIndex(text)
    spans: List[Tuple[int, int, str]] = []
    for e in edits:
        rng = e.get("range")
        if not isinstance(rng, dict):
            raise ValueError("Invalid edit: missing range")
        start = rng.get("start")
        end = rng.get("end")
        if not isinstance(start, dict) or not isinstance(end, dict):
            raise ValueError("Invalid edit: missing start/end")

        s = idx.offset(int(start.get("line", 0)), int(start.get("character", 0)))
        t = idx.offset(int(end.get("line", 0)), int(end.get("character", 0)))

        new_text = e.get("newText", "")
        if new_text is None:
            new_text = ""
        if not isinstance(new_text, str):
            new_text = str(new_text)

        spans.append((s, t, new_text))

    spans_sorted = sorted(spans, key=lambda x: (x[0], x[1]))
    for (s1, e1, _), (s2, e2, _) in zip(spans_sorted, spans_sorted[1:]):
        if s2 < e1:
            raise ValueError("Overlapping edits")

    out = text
    for s, t, new_text in sorted(spans, key=lambda x: x[0], reverse=True):
        out = out[:s] + new_text + out[t:]
    return out


def _ptr_child(parent: str, seg: str) -> str:
    return f"{parent}/{seg}" if parent else f"/{seg}"


def _ptr_child_escaped(parent: str, seg: str) -> str:
    """Build a child pointer with proper escaping for object keys."""
    segs = split_pointer(parent)
    segs.append(seg)
    return join_pointer(segs)


def _minimal_edits_for_single_patch(
    doc: "Document",
    before: Dict[str, Any],
    after: Dict[str, Any],
    op: Dict[str, Any],
) -> Optional[List[Dict[str, Any]]]:
    """Convert a *single* JSON Patch operation into minimal LSP text edits.

    We intentionally support only the patch shapes produced by Astra's deterministic
    repair suggestions (and a few safe extras):

    - replace /path -> replace the token span at /path
    - add into list (/path/- or /path/<idx>):
        * if list non-empty, insert at element token boundary
        * otherwise, replace the entire list token

    If we cannot confidently produce a small edit, return None so the caller can
    fall back to a full-document replacement.

    IMPORTANT: This is best-effort and intentionally conservative.
    """
    if not doc.spans:
        return None

    kind = op.get("op")
    path = op.get("path")
    if not isinstance(kind, str) or not isinstance(path, str):
        return None

    # --- helpers -------------------------------------------------

    def replace_pointer(ptr: str, new_value: Any) -> Optional[List[Dict[str, Any]]]:
        """Replace a pointer's *value* span with a JSON dump."""
        if ptr not in doc.spans:
            return None
        span = doc.spans[ptr]
        base_indent = _line_prefix(doc.text, span[0])
        dumped = json.dumps(new_value, ensure_ascii=False, indent=2)
        dumped = _indent_after_first_line(dumped, base_indent)
        return [_edit_replace_span(doc, span, dumped)]

    def delete_span(span: Tuple[int, int]) -> List[Dict[str, Any]]:
        return [_edit_replace_span(doc, span, "")]

    def _object_child_ptr(parent_ptr: str, key: str) -> str:
        return _ptr_child_escaped(parent_ptr, key)

    def _build_prop_block(prop_indent: str, key: str, value: Any) -> str:
        """Return multi-line property text starting at column 0 (indent is included)."""
        key_json = json.dumps(key, ensure_ascii=False)
        v_dump = json.dumps(value, ensure_ascii=False, indent=2)
        v_lines = v_dump.splitlines() or [v_dump]
        out: List[str] = []
        out.append(prop_indent + f"{key_json}: {v_lines[0]}")
        for ln in v_lines[1:]:
            out.append(prop_indent + ln)
        return "\n".join(out)

    def _build_prop_inline(key: str, value: Any) -> str:
        key_json = json.dumps(key, ensure_ascii=False)
        v_dump = json.dumps(value, ensure_ascii=False)
        return f"{key_json}: {v_dump}"

    # --- replace -------------------------------------------------

    if kind == "replace":
        return replace_pointer(path, op.get("value"))

    # --- add -----------------------------------------------------

    if kind == "add":
        segs = split_pointer(path)
        if not segs:
            return None  # root replacement is too risky
        parent_ptr = join_pointer(segs[:-1])
        last = segs[-1]

        try:
            parent_val = ptr_resolve(before, parent_ptr)
        except Exception:
            return None

        # 1) add into list
        if isinstance(parent_val, list):
            arr_len = len(parent_val)
            item_indent = ""
            if arr_len > 0:
                first_ptr = _ptr_child(parent_ptr, "0")
                if first_ptr in doc.spans:
                    item_indent = _line_prefix(doc.text, doc.spans[first_ptr][0])
            else:
                try:
                    new_list = ptr_resolve(after, parent_ptr)
                except Exception:
                    return None
                return replace_pointer(parent_ptr, new_list)

            if "value" not in op:
                return None
            new_item = op["value"]
            parent_span = doc.spans.get(parent_ptr) if doc.spans else None
            list_multiline = False
            if parent_span:
                list_multiline = "\n" in doc.text[parent_span[0] : parent_span[1]]
            dumped = json.dumps(new_item, ensure_ascii=False, indent=2)
            compact = json.dumps(new_item, ensure_ascii=False)

            # Append
            if last == "-" or (isinstance(last, str) and last.isdigit() and int(last) == arr_len):
                last_ptr = _ptr_child(parent_ptr, str(arr_len - 1))
                if last_ptr not in doc.spans:
                    try:
                        new_list = ptr_resolve(after, parent_ptr)
                    except Exception:
                        return None
                    return replace_pointer(parent_ptr, new_list)
                insert_at = doc.spans[last_ptr][1]
                if not list_multiline:
                    return [_edit_insert(doc, insert_at, ", " + compact)]
                inserted = _indent_all_lines(dumped, item_indent)
                return [_edit_insert(doc, insert_at, ",\n" + inserted)]

            # Insert before index
            if isinstance(last, str) and last.isdigit():
                ins_i = int(last)
                if ins_i < 0:
                    return None
                child_ptr = _ptr_child(parent_ptr, str(ins_i))
                if child_ptr not in doc.spans:
                    try:
                        new_list = ptr_resolve(after, parent_ptr)
                    except Exception:
                        return None
                    return replace_pointer(parent_ptr, new_list)
                insert_at = doc.spans[child_ptr][0]
                if not list_multiline:
                    return [_edit_insert(doc, insert_at, compact + ", ")]
                inserted = _indent_after_first_line(dumped, item_indent)
                return [_edit_insert(doc, insert_at, inserted + ",\n" + item_indent)]

            return None

        # 2) add into object (property add)
        if isinstance(parent_val, dict):
            if "value" not in op:
                return None
            new_value = op["value"]

            key = str(last)
            child_ptr = _object_child_ptr(parent_ptr, key)

            # RFC6902: add into object overwrites if key exists
            if key in parent_val:
                return replace_pointer(child_ptr, new_value)

            if parent_ptr not in doc.spans:
                # can't locate the object token -> fallback
                try:
                    new_obj = ptr_resolve(after, parent_ptr)
                except Exception:
                    return None
                return replace_pointer(parent_ptr, new_obj)

            obj_span = doc.spans[parent_ptr]
            obj_text = doc.text[obj_span[0] : obj_span[1]]
            multiline = "\n" in obj_text

            # Empty object: insert between braces.
            if len(parent_val) == 0:
                open_brace = doc.text.find("{", obj_span[0], min(obj_span[0] + 6, len(doc.text)))
                if open_brace == -1:
                    open_brace = obj_span[0]
                insert_at = open_brace + 1

                if multiline:
                    base_indent = _line_prefix(doc.text, obj_span[0])
                    prop_indent = base_indent + "  "
                    prop_block = _build_prop_block(prop_indent, key, new_value)
                    return [_edit_insert(doc, insert_at, "\n" + prop_block + "\n" + base_indent)]
                else:
                    return [_edit_insert(doc, insert_at, _build_prop_inline(key, new_value))]

            # Non-empty object: append as new last property.
            keys_in_order = list(parent_val.keys())
            last_key = keys_in_order[-1]
            last_ptr = _object_child_ptr(parent_ptr, str(last_key))
            first_ptr = _object_child_ptr(parent_ptr, str(keys_in_order[0]))

            if not doc.pair_spans or last_ptr not in doc.pair_spans:
                try:
                    new_obj = ptr_resolve(after, parent_ptr)
                except Exception:
                    return None
                return replace_pointer(parent_ptr, new_obj)

            insert_at = doc.pair_spans[last_ptr][1]
            prop_indent = ""
            if doc.pair_spans and first_ptr in doc.pair_spans:
                prop_indent = _line_prefix(doc.text, doc.pair_spans[first_ptr][0])
            else:
                prop_indent = _line_prefix(doc.text, obj_span[0]) + "  "

            if multiline:
                prop_block = _build_prop_block(prop_indent, key, new_value)
                return [_edit_insert(doc, insert_at, ",\n" + prop_block)]
            else:
                # One-line object: keep one-line if possible
                inline = _build_prop_inline(key, new_value)
                return [_edit_insert(doc, insert_at, ", " + inline)]

        return None

    # --- remove --------------------------------------------------

    if kind == "remove":
        segs = split_pointer(path)
        if not segs:
            return None
        parent_ptr = join_pointer(segs[:-1])
        last = segs[-1]

        try:
            parent_val = ptr_resolve(before, parent_ptr)
        except Exception:
            return None

        # remove from list
        if isinstance(parent_val, list):
            if not isinstance(last, str) or not last.isdigit():
                return None
            idx = int(last)
            if idx < 0 or idx >= len(parent_val):
                return None

            if len(parent_val) == 1:
                return replace_pointer(parent_ptr, [])

            # Need element spans.
            if idx == 0:
                p0 = _ptr_child(parent_ptr, "0")
                p1 = _ptr_child(parent_ptr, "1")
                if p0 in doc.spans and p1 in doc.spans:
                    return delete_span((doc.spans[p0][0], doc.spans[p1][0]))
                try:
                    new_list = ptr_resolve(after, parent_ptr)
                except Exception:
                    return None
                return replace_pointer(parent_ptr, new_list)

            prev = _ptr_child(parent_ptr, str(idx - 1))
            cur = _ptr_child(parent_ptr, str(idx))
            if prev in doc.spans and cur in doc.spans:
                return delete_span((doc.spans[prev][1], doc.spans[cur][1]))

            try:
                new_list = ptr_resolve(after, parent_ptr)
            except Exception:
                return None
            return replace_pointer(parent_ptr, new_list)

        # remove property from object
        if isinstance(parent_val, dict):
            key = str(last)
            if key not in parent_val:
                return None

            if len(parent_val) == 1:
                return replace_pointer(parent_ptr, {})

            if not doc.pair_spans:
                try:
                    new_obj = ptr_resolve(after, parent_ptr)
                except Exception:
                    return None
                return replace_pointer(parent_ptr, new_obj)

            keys_in_order = list(parent_val.keys())
            try:
                idx = keys_in_order.index(key)
            except ValueError:
                return None

            child_ptrs = [_object_child_ptr(parent_ptr, str(k)) for k in keys_in_order]
            if any(cp not in doc.pair_spans for cp in child_ptrs):
                try:
                    new_obj = ptr_resolve(after, parent_ptr)
                except Exception:
                    return None
                return replace_pointer(parent_ptr, new_obj)

            if idx == 0:
                start = doc.pair_spans[child_ptrs[0]][0]
                end = doc.pair_spans[child_ptrs[1]][0]
                return delete_span((start, end))

            start = doc.pair_spans[child_ptrs[idx - 1]][1]
            end = doc.pair_spans[child_ptrs[idx]][1]
            return delete_span((start, end))

        return None

    # other ops: not supported
    return None



# -----------------
# Fix-all minimal edits (multi-patch)
# -----------------


def _span_overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    """Return True if two half-open spans [a0,a1) and [b0,b1) overlap."""
    return a[0] < b[1] and b[0] < a[1]


def _common_prefix_pointer(ptrs: List[str]) -> str:
    """Compute the deepest common ancestor pointer for a list of pointers."""
    if not ptrs:
        return ""
    seg_lists = [split_pointer(p) for p in ptrs]
    prefix: List[str] = []
    for i in range(min(len(s) for s in seg_lists)):
        seg = seg_lists[0][i]
        if all(s[i] == seg for s in seg_lists[1:]):
            prefix.append(seg)
        else:
            break
    return join_pointer(prefix)


def _replace_value_at_pointer(doc: "Document", ptr: str, new_value: Any) -> Optional[List[Dict[str, Any]]]:
    """Replace the JSON *value token* at `ptr` using recorded spans.

    This replaces only the value span (e.g. the `[ ... ]` part), not the full
    key/value pair. It is safe and works well for fix-all actions.
    """
    if not doc.spans or ptr not in doc.spans:
        return None
    span = doc.spans[ptr]
    base_indent = _line_prefix(doc.text, span[0])
    dumped = json.dumps(new_value, ensure_ascii=False, indent=2)
    dumped = _indent_after_first_line(dumped, base_indent)
    return [_edit_replace_span(doc, span, dumped)]


def _container_pointer_for_patch(op: Dict[str, Any]) -> str:
    """Return a conservative 'container pointer' that the patch logically mutates.

    - replace /x/y      -> /x/y (value)
    - add/remove /x/y/k -> /x/y (container)

    This is used for safe grouping + overlap detection.
    """
    kind = op.get('op')
    path = op.get('path', '')
    if not isinstance(path, str):
        return ''
    if kind == 'replace':
        return path
    segs = split_pointer(path)
    if not segs:
        return ''
    return join_pointer(segs[:-1])


def _minimal_edits_for_patch_list_grouped(
    doc: "Document",
    before: Dict[str, Any],
    patch_ops: List[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """Best-effort minimal edits for a JSON Patch list.

    Strategy:
    1) Group operations by conservative container pointers.
    2) For groups with a single op, try the existing single-op minimal edit.
       If it fails, fall back to a container value replacement.
    3) For groups with multiple ops, replace the container value.
    4) Merge overlapping groups by promoting them to a common ancestor pointer
       and replacing that ancestor value.

    Returns list of LSP TextEdits or None to indicate fallback to full-document
    replacement.
    """
    if not doc.spans:
        return None

    # Keep stable ordering (RFC6902 patch order matters).
    indexed = [(i, op) for i, op in enumerate(patch_ops) if isinstance(op, dict)]
    if not indexed:
        return []

    # Build groups by container pointer.
    groups: Dict[str, List[Tuple[int, Dict[str, Any]]]] = {}
    for idx, op in indexed:
        cptr = _container_pointer_for_patch(op)
        groups.setdefault(cptr, []).append((idx, op))

    # Build group edits.
    built_groups: List[Dict[str, Any]] = []

    for cptr, items in groups.items():
        # Ensure we can locate the container in the original text.
        if cptr not in doc.spans:
            # If container doesn't exist in spans, we're not confident.
            return None

        # Apply only this group's patches to compute replacement values.
        patch_list = [op for _i, op in sorted(items, key=lambda t: t[0])]
        after_group = apply_patch(json.loads(json.dumps(before)), patch_list)

        # Compute new container value (for replacement fallback).
        try:
            if cptr == '':
                new_container_val = after_group
            else:
                new_container_val = ptr_resolve(after_group, cptr)
        except Exception:
            return None

        # Decide whether to use a single-op minimal edit or container replacement.
        edits: Optional[List[Dict[str, Any]]] = None
        if len(patch_list) == 1:
            # compute after_single for this op
            after_single = after_group
            edits = _minimal_edits_for_single_patch(doc, before, after_single, patch_list[0])

        if edits is None:
            edits = _replace_value_at_pointer(doc, cptr, new_container_val)

        if edits is None:
            return None

        span = doc.spans[cptr]
        built_groups.append({
            'container': cptr,
            'items': items,  # (idx, op)
            'span': span,
            'edits': edits,
        })

    # Merge overlapping groups by promoting to a common ancestor pointer.
    built_groups.sort(key=lambda g: g['span'][0])

    def _merge_two(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # merge patch items preserving original order
        merged_items = {i: op for i, op in a['items']}
        for i, op in b['items']:
            merged_items[i] = op
        merged_list = [op for i, op in sorted(merged_items.items(), key=lambda t: t[0])]

        # choose ancestor pointer
        ancestor = _common_prefix_pointer([a['container'], b['container']])
        # walk up until we find a span (root '' is allowed for fix-all)
        cur = ancestor
        while cur not in doc.spans:
            if cur == '':
                return None
            if '/' not in cur:
                cur = ''
            else:
                cur = cur.rsplit('/', 1)[0]

        after_merged = apply_patch(json.loads(json.dumps(before)), merged_list)
        try:
            if cur == '':
                new_val = after_merged
            else:
                new_val = ptr_resolve(after_merged, cur)
        except Exception:
            return None

        edits = _replace_value_at_pointer(doc, cur, new_val)
        if edits is None:
            return None

        return {
            'container': cur,
            'items': sorted(merged_items.items(), key=lambda t: t[0]),
            'span': doc.spans[cur],
            'edits': edits,
        }

    merged: List[Dict[str, Any]] = []
    for g in built_groups:
        if not merged:
            merged.append(g)
            continue
        last = merged[-1]
        if _span_overlaps(last['span'], g['span']):
            m = _merge_two(last, g)
            if m is None:
                return None
            merged[-1] = m
        else:
            merged.append(g)

    # One more pass in case merges created new overlaps.
    changed = True
    while changed:
        changed = False
        merged.sort(key=lambda g: g['span'][0])
        out: List[Dict[str, Any]] = []
        for g in merged:
            if not out:
                out.append(g)
                continue
            last = out[-1]
            if _span_overlaps(last['span'], g['span']):
                m = _merge_two(last, g)
                if m is None:
                    return None
                out[-1] = m
                changed = True
            else:
                out.append(g)
        merged = out

    # Emit edits in descending order of spans (helps clients apply without offset issues).
    merged.sort(key=lambda g: g['span'][0], reverse=True)
    all_edits: List[Dict[str, Any]] = []
    for g in merged:
        all_edits.extend(g['edits'])

    return all_edits


def _minimal_edits_for_patch_list(
    doc: "Document",
    before: Dict[str, Any],
    patch_ops: List[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """Attempt token-level minimal edits for a *patch list*.

    Compared to the conservative grouped strategy, this tries to emit minimal edits
    for each patch op (even when multiple ops touch the same container), as long as
    the resulting TextEdits are non-overlapping.

    If anything looks risky (unsupported op shapes, overlapping edits, missing spans),
    we fall back to the conservative container-grouped strategy.

    NOTE: This is best-effort. Correctness is prioritized over minimality.
    """
    if not doc.spans:
        return None
    if doc.index is None:
        doc.index = jsonpos.TextIndex(doc.text)

    # First pass: per-op minimal edits (token-level).
    per_edits: List[Dict[str, Any]] = []
    for op in patch_ops:
        if not isinstance(op, dict):
            continue
        try:
            # compute after for this single op (relative to the *original* AST)
            after_single = apply_patch(json.loads(json.dumps(before)), [op])
        except Exception:
            return _minimal_edits_for_patch_list_grouped(doc, before, patch_ops)

        e = _minimal_edits_for_single_patch(doc, before, after_single, op)
        if e is None:
            return _minimal_edits_for_patch_list_grouped(doc, before, patch_ops)
        per_edits.extend(e)

    if not per_edits:
        return []

    # Coalesce multiple inserts at the same position (common for multiple appends).
    insert_map: Dict[Tuple[int, int], Dict[str, Any]] = {}
    other: List[Tuple[int, int, Dict[str, Any]]] = []

    for e in per_edits:
        rng = e.get('range')
        if not isinstance(rng, dict):
            return _minimal_edits_for_patch_list_grouped(doc, before, patch_ops)
        s = rng.get('start')
        t = rng.get('end')
        if not isinstance(s, dict) or not isinstance(t, dict):
            return _minimal_edits_for_patch_list_grouped(doc, before, patch_ops)

        si = doc.index.offset(int(s.get('line', 0)), int(s.get('character', 0)))
        ti = doc.index.offset(int(t.get('line', 0)), int(t.get('character', 0)))

        nt = e.get('newText', '')
        if nt is None:
            nt = ''
        if not isinstance(nt, str):
            nt = str(nt)

        if si == ti:
            key = (si, ti)
            if key not in insert_map:
                insert_map[key] = {'range': rng, 'newText': ''}
            # Preserve patch order by concatenating in the original op order.
            insert_map[key]['newText'] += nt
        else:
            other.append((si, ti, {'range': rng, 'newText': nt}))

    merged: List[Tuple[int, int, Dict[str, Any]]] = other[:]
    for (si, ti), e in insert_map.items():
        merged.append((si, ti, e))

    # Overlap check (in absolute index space).
    merged_sorted = sorted(merged, key=lambda x: (x[0], x[1]))
    for (s1, e1, _), (s2, e2, _) in zip(merged_sorted, merged_sorted[1:]):
        if s2 < e1:
            return _minimal_edits_for_patch_list_grouped(doc, before, patch_ops)

    # Return in descending order of start offsets (safer for many clients).
    merged_sorted_desc = sorted(merged, key=lambda x: x[0], reverse=True)
    return [e for _s, _t, e in merged_sorted_desc]


@dataclass(frozen=True)
class IssueSummary:
    """Summary of non-schema analysis issues for delta validation.

    We key issues primarily by (code, pointer) so we can perform
    diagnostic-targeted validation (i.e., ensure a specific issue disappears).
    """

    errors: Set[Tuple[str, str]]
    warnings: Set[Tuple[str, str]]


@dataclass
class Document:
    uri: str
    text: str
    version: int = 0
    module: Optional[Dict[str, Any]] = None
    spans: Optional[jsonpos.PointerSpans] = None
    pair_spans: Optional[jsonpos.PointerSpans] = None
    index: Optional[jsonpos.TextIndex] = None


class AstraLSP:
    def __init__(self) -> None:
        self.docs: Dict[str, Document] = {}
        self.shutdown_requested = False
        # Cache schema for faster diagnostics + quick-fix pre-validation.
        self.schema = fmt.load_schema()

    def _summarize_non_schema(self, mod: Dict[str, Any]) -> IssueSummary:
        """Compute a lightweight issue summary (excluding schema errors)."""
        errors: Set[Tuple[str, str]] = set()
        warnings: Set[Tuple[str, str]] = set()
        for iss in (semantic.check_module(mod) + typecheck.check_module(mod) + effects.check_effects(mod)):
            if not isinstance(iss, dict):
                continue
            sev = iss.get("severity", "error")
            code = iss.get("code", "Issue")
            ptr = iss.get("pointer", "") or ""
            if not isinstance(code, str):
                code = str(code)
            if not isinstance(ptr, str):
                ptr = str(ptr)
            key = (code, ptr)
            if sev == "warning":
                warnings.add(key)
            else:
                errors.add(key)
        return IssueSummary(errors=errors, warnings=warnings)

    def _no_regression(self, base: IssueSummary, new: IssueSummary) -> bool:
        """Return True if `new` introduces no new errors (and no new warnings when errors are unchanged)."""
        if not new.errors.issubset(base.errors):
            return False
        if len(new.errors) == len(base.errors) and not new.warnings.issubset(base.warnings):
            return False
        # Do not offer no-op quick fixes.
        if new.errors == base.errors and new.warnings == base.warnings:
            return False
        return True

    def _edits_pass_prevalidation(
        self,
        doc: Document,
        edits: List[Dict[str, Any]],
        baseline: Optional[IssueSummary] = None,
        target: Optional[Tuple[str, str]] = None,
        expected_canonical: Optional[str] = None,
    ) -> bool:
        """Pre-validate a prospective code action edit.

        Steps:
        - apply edits in-memory (UTF-16 aware)
        - JSON parse
        - Astra schema validate (must pass)
        - optional delta validation: run semantic/type/effects checks and reject edits
          that introduce new errors (and new warnings when errors are unchanged).
        """
        try:
            new_text = _apply_text_edits_in_memory(doc.text, edits)
            value = json.loads(new_text)
            if not isinstance(value, dict):
                return False
            if len(fmt.validate(value, self.schema)) != 0:
                return False

            # If the caller provided the canonical text of the expected AST
            # (e.g. for fix-all), enforce semantic equivalence. This prevents
            # token-level edits from accidentally producing a different but valid AST.
            if expected_canonical is not None:
                try:
                    if fmt.dumps_canonical(value) != expected_canonical:
                        return False
                except Exception:
                    return False
            if baseline is None:
                return True
            new_sum = self._summarize_non_schema(value)
            # Ensure the targeted diagnostic disappears (diagnostic-based validation).
            if target is not None and (target in baseline.errors or target in baseline.warnings):
                if target in new_sum.errors or target in new_sum.warnings:
                    return False
            return self._no_regression(baseline, new_sum)
        except Exception:
            return False

    def _parse_doc(self, doc: Document) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]]]:
        """Parse JSON and attach pointer->span info.

        Returns: (module | None, error_message | None, error_range | None)
        """
        try:
            value, spans, pair_spans = jsonpos.parse_with_positions(doc.text)
            if isinstance(value, dict):
                doc.module = value
                doc.spans = spans
                doc.pair_spans = pair_spans
                doc.index = jsonpos.TextIndex(doc.text)
                return value, None, None
            return None, "Top-level JSON must be an object", _full_range(doc.text)
        except jsonpos.JsonPosError as e:
            # pinpoint parse error position
            idx = jsonpos.TextIndex(doc.text)
            pos = idx.position(e.index)
            rng = {"start": pos, "end": idx.position(min(e.index + 1, len(doc.text)))}
            return None, str(e), rng
        except Exception as e:
            return None, str(e), _full_range(doc.text)

    def _range_for_pointer(self, doc: Document, pointer: str) -> Dict[str, Any]:
        """Best-effort mapping from JSON pointer to an LSP range."""
        if not doc.spans or not doc.index:
            return _full_range(doc.text)

        p = pointer or ""
        if p == "/":
            p = ""

        # Walk up pointer segments until we find a recorded span.
        cur = p
        while True:
            # Prefer property (key/value pair) spans for object members.
            if doc.pair_spans and cur in doc.pair_spans:
                return jsonpos.span_to_lsp_range(doc.text, doc.pair_spans[cur], doc.index)
            if cur in doc.spans:
                return jsonpos.span_to_lsp_range(doc.text, doc.spans[cur], doc.index)
            if cur == "":
                break
            if "/" not in cur:
                cur = ""
                continue
            cur = cur.rsplit("/", 1)[0]
        return _full_range(doc.text)

    def _diagnostics_for(self, doc: Document) -> List[Dict[str, Any]]:
        mod, err, err_range = self._parse_doc(doc)
        full = _full_range(doc.text)
        diags: List[Dict[str, Any]] = []

        if err:
            diags.append(
                {
                    "range": err_range or full,
                    "severity": 1,
                    "source": "astra",
                    "code": "JSONParse",
                    "data": {"pointer": "", "code": "JSONParse"},
                    "message": err,
                }
            )
            return diags

        # schema validate
        try:
            s_errs = fmt.validate(mod, self.schema)
            for e in s_errs:
                ptr = e.get("pointer", "") or ""
                diags.append(
                    {
                        "range": self._range_for_pointer(doc, ptr),
                        "severity": 1,
                        "source": "astra",
                        "code": "SchemaError",
                        "data": {"pointer": ptr, "code": "SchemaError"},
                        "message": f"{ptr}: {e.get('message','schema error')}",
                    }
                )
        except Exception:
            diags.append(
                {
                    "range": full,
                    "severity": 1,
                    "source": "astra",
                    "code": "SchemaInternal",
                    "message": "Internal schema validation error",
                }
            )

        # semantic/type/effects
        for iss in semantic.check_module(mod) + typecheck.check_module(mod) + effects.check_effects(mod):
            ptr = iss.get("pointer", "") or ""
            diags.append(
                {
                    "range": self._range_for_pointer(doc, ptr),
                    "severity": _severity(iss.get("severity", "error")),
                    "source": "astra",
                    "code": iss.get("code"),
                    "data": {"pointer": ptr, "code": iss.get("code")},
                    "message": f"{ptr}: {iss.get('message','')}",
                }
            )

        return diags

    def publish_diagnostics(self, uri: str) -> None:
        doc = self.docs.get(uri)
        if not doc:
            return
        diags = self._diagnostics_for(doc)
        send_message(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": diags},
            }
        )

    # -----------------
    # Request handlers
    # -----------------

    def on_initialize(self, msg: Json) -> None:
        req_id = msg.get("id")
        result = {
            "capabilities": {
                "textDocumentSync": 1,
                "completionProvider": {"resolveProvider": False},
                "documentFormattingProvider": True,
                "codeActionProvider": True,
            }
        }
        send_message({"jsonrpc": "2.0", "id": req_id, "result": result})

    def on_shutdown(self, msg: Json) -> None:
        self.shutdown_requested = True
        send_message({"jsonrpc": "2.0", "id": msg.get("id"), "result": None})

    def on_did_open(self, msg: Json) -> None:
        td = msg.get("params", {}).get("textDocument", {})
        uri = td.get("uri")
        text = td.get("text", "")
        ver = td.get("version", 0)
        if isinstance(uri, str):
            self.docs[uri] = Document(uri=uri, text=text, version=int(ver) if isinstance(ver, int) else 0)
            self.publish_diagnostics(uri)

    def on_did_change(self, msg: Json) -> None:
        params = msg.get("params", {})
        td = params.get("textDocument", {})
        uri = td.get("uri")
        if not isinstance(uri, str):
            return
        changes = params.get("contentChanges", []) or []
        if not isinstance(changes, list) or not changes:
            return
        # Support full-document sync (common)
        new_text = changes[-1].get("text")
        if not isinstance(new_text, str):
            return
        doc = self.docs.get(uri)
        if not doc:
            doc = Document(uri=uri, text=new_text, version=0)
            self.docs[uri] = doc
        else:
            doc.text = new_text
        self.publish_diagnostics(uri)

    def on_completion(self, msg: Json) -> None:
        req_id = msg.get("id")
        params = msg.get("params", {})
        td = params.get("textDocument", {})
        uri = td.get("uri")
        items: List[Dict[str, Any]] = []

        # builtins
        for b in sorted(rt.BUILTINS.keys()):
            items.append({"label": b, "kind": 3})  # Function

        # module functions if parseable
        if isinstance(uri, str) and uri in self.docs:
            mod, _, _ = self._parse_doc(self.docs[uri])
            if isinstance(mod, dict):
                for f in mod.get("functions", []) or []:
                    if isinstance(f, dict) and isinstance(f.get("name"), str):
                        items.append({"label": f["name"], "kind": 3})

        send_message({"jsonrpc": "2.0", "id": req_id, "result": {"isIncomplete": False, "items": items}})

    def on_formatting(self, msg: Json) -> None:
        req_id = msg.get("id")
        params = msg.get("params", {})
        td = params.get("textDocument", {})
        uri = td.get("uri")
        if not isinstance(uri, str) or uri not in self.docs:
            send_message({"jsonrpc": "2.0", "id": req_id, "result": []})
            return
        doc = self.docs[uri]
        mod, err, _ = self._parse_doc(doc)
        if err or not isinstance(mod, dict):
            send_message({"jsonrpc": "2.0", "id": req_id, "result": []})
            return
        new_text = fmt.dumps_canonical(mod)
        edit = {"range": _full_range(doc.text), "newText": new_text}
        send_message({"jsonrpc": "2.0", "id": req_id, "result": [edit]})

    def on_code_action(self, msg: Json) -> None:
        req_id = msg.get("id")
        params = msg.get("params", {})
        td = params.get("textDocument", {})
        uri = td.get("uri")
        if not isinstance(uri, str) or uri not in self.docs:
            send_message({"jsonrpc": "2.0", "id": req_id, "result": []})
            return
        doc = self.docs[uri]
        mod, err, _ = self._parse_doc(doc)
        if err or not isinstance(mod, dict):
            send_message({"jsonrpc": "2.0", "id": req_id, "result": []})
            return

        try:
            baseline = self._summarize_non_schema(mod)
        except Exception:
            baseline = None


        actions: List[Dict[str, Any]] = []

        # Requested range + diagnostics in that range
        req_range = params.get("range") if isinstance(params.get("range"), dict) else None
        ctx = params.get("context", {}) if isinstance(params.get("context"), dict) else {}
        ctx_diags = ctx.get("diagnostics", []) if isinstance(ctx.get("diagnostics", []), list) else []

        full_rng = _full_range(doc.text)

        # Always offer canonical formatting.
        canonical_text = fmt.dumps_canonical(mod)
        actions.append({
            "title": "Astra: Format (canonical)",
            "kind": "source.format",
            "edit": {"changes": {uri: [{"range": full_rng, "newText": canonical_text}]}},
        })

        supported = {"MissingReturn", "NotPure", "MissingEffect"}
        offered_any = False

        for d in ctx_diags:
            if not isinstance(d, dict):
                continue
            d_range = d.get("range") if isinstance(d.get("range"), dict) else None
            if req_range and d_range and not _range_intersects(req_range, d_range):
                continue

            data = d.get("data") if isinstance(d.get("data"), dict) else {}
            code = data.get("code") or d.get("code")
            ptr = data.get("pointer") or ""
            if not isinstance(code, str) or code not in supported:
                continue

            issue = {"code": code, "pointer": ptr}
            patch = suggest_patches(mod, [issue])
            if not patch:
                continue

            fixed = apply_patch(json.loads(json.dumps(mod)), patch)
            expected_fixed = fmt.dumps_canonical(fixed)

            # Minimal edit attempt
            edits: Optional[List[Dict[str, Any]]] = None
            if len(patch) == 1:
                edits = _minimal_edits_for_single_patch(doc, mod, fixed, patch[0])
            if edits is None:
                edits = [{"range": full_rng, "newText": fmt.dumps_canonical(fixed)}]

            # Delta-aware prevalidation; if minimal fails, try canonical full replace.
            target = (code, str(ptr or ""))
            if not self._edits_pass_prevalidation(doc, edits, baseline, target=target, expected_canonical=expected_fixed):
                fallback = [{"range": full_rng, "newText": expected_fixed}]
                if not self._edits_pass_prevalidation(doc, fallback, baseline, target=target, expected_canonical=expected_fixed):
                    continue
                edits = fallback

            # Title
            title = "Astra: Quick fix"
            if code == "MissingReturn":
                title = "Astra: Add missing return"
            elif code == "NotPure":
                title = "Astra: Adjust effects (not pure)"
            elif code == "MissingEffect":
                title = "Astra: Add missing effects"

            actions.append({
                "title": title,
                "kind": "quickfix",
                "diagnostics": [d],
                "isPreferred": True,
                "edit": {"changes": {uri: edits}},
            })

            # Convenience: fix + canonical format (full replace)
            fmt_edits = [{"range": full_rng, "newText": expected_fixed}]
            if self._edits_pass_prevalidation(doc, fmt_edits, baseline, target=target, expected_canonical=expected_fixed):
                actions.append({
                    "title": title + " + Format",
                    "kind": "quickfix",
                    "diagnostics": [d],
                    "isPreferred": False,
                    "edit": {"changes": {uri: fmt_edits}},
                })

            offered_any = True

        # Fix-all (deterministic)
        if offered_any:
            issues0 = semantic.check_module(mod) + typecheck.check_module(mod) + effects.check_effects(mod)
            patch_all = suggest_patches(mod, issues0)
            if patch_all:
                fixed_all = apply_patch(json.loads(json.dumps(mod)), patch_all)

                # Idempotence check: after applying deterministic patches,
                # running suggest_patches again should not propose additional
                # deterministic patches. If it does, we iteratively apply
                # suggestions (bounded) to reach a fixed point.
                stable = fixed_all
                passes = 0
                try:
                    issues1 = semantic.check_module(stable) + typecheck.check_module(stable) + effects.check_effects(stable)
                    patch_next = suggest_patches(stable, issues1)
                except Exception:
                    patch_next = []

                # Apply additional passes until no new patches (fixed point).
                while patch_next and passes < 5:
                    passes += 1
                    stable = apply_patch(json.loads(json.dumps(stable)), patch_next)
                    try:
                        issues1 = semantic.check_module(stable) + typecheck.check_module(stable) + effects.check_effects(stable)
                        patch_next = suggest_patches(stable, issues1)
                    except Exception:
                        patch_next = []

                # If we still have patches after the bound, do not offer fix-all.
                if patch_next:
                    stable = None

                if stable is not None:
                    fixed_all = stable
                    expected_fixed_all = fmt.dumps_canonical(fixed_all)

                    # If we reached the fixed point in a single pass, we can try
                    # token-level minimal edits from patch_all. Otherwise, we fall
                    # back to a safe full-document replacement (still prevalidated).
                    edits_all = None
                    if passes == 0:
                        edits_all = _minimal_edits_for_patch_list(doc, mod, patch_all)
                    if edits_all is None:
                        edits_all = [{"range": full_rng, "newText": expected_fixed_all}]

                    if not self._edits_pass_prevalidation(doc, edits_all, baseline, expected_canonical=expected_fixed_all):
                        fallback_all = [{"range": full_rng, "newText": expected_fixed_all}]
                        if not self._edits_pass_prevalidation(doc, fallback_all, baseline, expected_canonical=expected_fixed_all):
                            edits_all = None
                        else:
                            edits_all = fallback_all

                    if edits_all is not None:
                        actions.append({
                            "title": "Astra: Fix all (deterministic, minimal)",
                            "kind": "source.fixAll",
                            "edit": {"changes": {uri: edits_all}},
                        })

                        fmt_all = [{"range": full_rng, "newText": expected_fixed_all}]
                        if self._edits_pass_prevalidation(doc, fmt_all, baseline, expected_canonical=expected_fixed_all):
                            actions.append({
                                "title": "Astra: Fix all (deterministic) + Format",
                                "kind": "source.fixAll",
                                "edit": {"changes": {uri: fmt_all}},
                            })
        send_message({"jsonrpc": "2.0", "id": req_id, "result": actions})


    # -----------------

    def handle(self, msg: Json) -> None:
        method = msg.get("method")
        if method == "initialize":
            return self.on_initialize(msg)
        if method == "shutdown":
            return self.on_shutdown(msg)
        if method == "exit":
            # no response
            raise SystemExit(0)

        if method == "textDocument/didOpen":
            return self.on_did_open(msg)
        if method == "textDocument/didChange":
            return self.on_did_change(msg)

        if method == "textDocument/completion":
            return self.on_completion(msg)
        if method == "textDocument/formatting":
            return self.on_formatting(msg)
        if method == "textDocument/codeAction":
            return self.on_code_action(msg)

        # default: if request, respond null
        if "id" in msg:
            send_message({"jsonrpc": "2.0", "id": msg.get("id"), "result": None})


def main() -> int:
    server = AstraLSP()
    while True:
        try:
            msg = read_message()
            if msg is None:
                break
            server.handle(msg)
        except SystemExit:
            break
        except Exception:
            # Avoid crashing the LSP; send minimal error if request.
            try:
                if isinstance(msg, dict) and "id" in msg:
                    send_message({"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": "Internal error"}})
            except Exception:
                pass
            # Also print to stderr for debugging
            traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
