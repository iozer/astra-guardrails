"""Position-aware JSON parser for LSP diagnostics.

We need to map JSON Pointers (RFC 6901) to editor ranges (line/character) so
diagnostics can point at the exact AST node that triggered an issue.

Python's built-in ``json`` module does not expose token/AST positions, so we
implement a small JSON parser that:

1) Parses JSON into Python objects (dict/list/scalars)
2) Records a span (start_index, end_index) in the source text for every node
3) Associates each span with a JSON Pointer for that node

This module is intentionally dependency-free.

Limitations / notes:
- Strict JSON only (no comments, trailing commas, etc.).
- Ranges are calculated in terms of Python string indices (codepoints). LSP
  expects UTF-16 code units for the ``character`` field; conversion helpers are
  provided.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Optional

from astra.tools.pointer import escape_segment


Span = Tuple[int, int]
PointerSpans = Dict[str, Span]


@dataclass
class JsonPosError(Exception):
    """Parse error with a stable character offset."""

    message: str
    index: int

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.message} at index {self.index}"


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.n = len(text)
        self.i = 0
        self.spans: PointerSpans = {}
        # For object properties, record a span that covers the entire
        # key/value pair (e.g. "name": "sum"), not just the value.
        #
        # We store these spans keyed by the *child pointer* (the pointer
        # that normally resolves to the value). This allows diagnostics
        # that point at /obj/key to highlight the full property.
        self.pair_spans: PointerSpans = {}

    def _peek(self) -> str:
        return self.text[self.i] if self.i < self.n else ""

    def _consume(self, ch: str) -> None:
        if self._peek() != ch:
            raise JsonPosError(f"Expected {ch!r}", self.i)
        self.i += 1

    def _skip_ws(self) -> None:
        while self.i < self.n and self.text[self.i] in " \t\r\n":
            self.i += 1

    def parse(self) -> Any:
        self._skip_ws()
        val = self._parse_value("")
        self._skip_ws()
        if self.i != self.n:
            raise JsonPosError("Trailing characters", self.i)
        return val

    def _parse_value(self, ptr: str) -> Any:
        self._skip_ws()
        start = self.i
        ch = self._peek()
        if ch == "{":
            val = self._parse_object(ptr, start)
            return val
        if ch == "[":
            val = self._parse_array(ptr, start)
            return val
        if ch == '"':
            s = self._parse_string()
            end = self.i
            self.spans[ptr] = (start, end)
            return s
        if ch in "-0123456789":
            num = self._parse_number()
            end = self.i
            self.spans[ptr] = (start, end)
            return num
        # literals
        if self.text.startswith("true", self.i):
            self.i += 4
            end = self.i
            self.spans[ptr] = (start, end)
            return True
        if self.text.startswith("false", self.i):
            self.i += 5
            end = self.i
            self.spans[ptr] = (start, end)
            return False
        if self.text.startswith("null", self.i):
            self.i += 4
            end = self.i
            self.spans[ptr] = (start, end)
            return None
        raise JsonPosError("Invalid value", self.i)

    def _parse_object(self, ptr: str, start: int) -> Dict[str, Any]:
        self._consume("{")
        obj: Dict[str, Any] = {}
        self._skip_ws()
        if self._peek() == "}":
            self.i += 1
            self.spans[ptr] = (start, self.i)
            return obj

        while True:
            self._skip_ws()
            if self._peek() != '"':
                raise JsonPosError("Expected string key", self.i)
            key_start = self.i
            key = self._parse_string()
            self._skip_ws()
            self._consume(":")
            # child pointer
            seg = escape_segment(key)
            child_ptr = f"{ptr}/{seg}" if ptr else f"/{seg}"
            val = self._parse_value(child_ptr)
            obj[key] = val

            # Record the full key/value pair span for the child pointer.
            # Example: for pointer /a/b, span covers '"b": <value>'.
            # We intentionally include any whitespace between key, colon and value.
            try:
                _, child_end = self.spans.get(child_ptr, (key_start, self.i))
                self.pair_spans[child_ptr] = (key_start, child_end)
            except Exception:
                # Best-effort only.
                pass
            self._skip_ws()
            if self._peek() == "}":
                self.i += 1
                break
            self._consume(",")

        self.spans[ptr] = (start, self.i)
        return obj

    def _parse_array(self, ptr: str, start: int) -> List[Any]:
        self._consume("[")
        arr: List[Any] = []
        self._skip_ws()
        if self._peek() == "]":
            self.i += 1
            self.spans[ptr] = (start, self.i)
            return arr

        idx = 0
        while True:
            child_ptr = f"{ptr}/{idx}" if ptr else f"/{idx}"
            val = self._parse_value(child_ptr)
            arr.append(val)
            idx += 1
            self._skip_ws()
            if self._peek() == "]":
                self.i += 1
                break
            self._consume(",")
            self._skip_ws()

        self.spans[ptr] = (start, self.i)
        return arr

    def _parse_string(self) -> str:
        self._consume('"')
        out_chars: List[str] = []
        while True:
            if self.i >= self.n:
                raise JsonPosError("Unterminated string", self.i)
            ch = self.text[self.i]
            self.i += 1
            if ch == '"':
                break
            if ch == "\\":
                if self.i >= self.n:
                    raise JsonPosError("Unterminated escape", self.i)
                esc = self.text[self.i]
                self.i += 1
                if esc in '"\\/':
                    out_chars.append(esc)
                elif esc == "b":
                    out_chars.append("\b")
                elif esc == "f":
                    out_chars.append("\f")
                elif esc == "n":
                    out_chars.append("\n")
                elif esc == "r":
                    out_chars.append("\r")
                elif esc == "t":
                    out_chars.append("\t")
                elif esc == "u":
                    # 4 hex digits
                    if self.i + 4 > self.n:
                        raise JsonPosError("Invalid unicode escape", self.i)
                    hexs = self.text[self.i : self.i + 4]
                    self.i += 4
                    try:
                        out_chars.append(chr(int(hexs, 16)))
                    except Exception:
                        raise JsonPosError("Invalid unicode escape", self.i)
                else:
                    raise JsonPosError("Invalid escape", self.i)
            else:
                out_chars.append(ch)
        return "".join(out_chars)

    def _parse_number(self) -> Any:
        start = self.i
        if self._peek() == "-":
            self.i += 1
        if self.i >= self.n:
            raise JsonPosError("Invalid number", self.i)
        if self._peek() == "0":
            self.i += 1
        else:
            if not self._peek().isdigit():
                raise JsonPosError("Invalid number", self.i)
            while self.i < self.n and self._peek().isdigit():
                self.i += 1
        # fractional
        if self._peek() == ".":
            self.i += 1
            if not self._peek().isdigit():
                raise JsonPosError("Invalid number", self.i)
            while self.i < self.n and self._peek().isdigit():
                self.i += 1
        # exponent
        if self._peek() in "eE":
            self.i += 1
            if self._peek() in "+-":
                self.i += 1
            if not self._peek().isdigit():
                raise JsonPosError("Invalid number", self.i)
            while self.i < self.n and self._peek().isdigit():
                self.i += 1
        raw = self.text[start : self.i]
        try:
            if any(c in raw for c in ".eE"):
                return float(raw)
            return int(raw)
        except Exception:
            raise JsonPosError("Invalid number", start)


def parse_with_positions(text: str) -> Tuple[Any, PointerSpans, PointerSpans]:
    """Parse JSON and return:

    - value: parsed Python value
    - spans: pointer -> node span (value span)
    - pair_spans: pointer -> *property span* for object members
      (covers the key/value pair)
    """
    p = _Parser(text)
    value = p.parse()
    return value, p.spans, p.pair_spans


def parse_with_spans(text: str) -> Tuple[Any, PointerSpans]:
    """Backwards-compatible API: Parse JSON and return (value, spans)."""
    value, spans, _pairs = parse_with_positions(text)
    return value, spans


class TextIndex:
    """Helper to convert absolute string offsets to LSP (line, utf16-char).

    - ``line`` is 0-based
    - ``character`` is measured in UTF-16 code units (LSP spec)

    We precompute line starts to keep conversions cheap.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.starts: List[int] = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self.starts.append(i + 1)

        # Precompute line end indices for slicing
        self.ends: List[int] = []
        for s in self.starts:
            # find newline for this line
            nl = text.find("\n", s)
            self.ends.append(nl if nl != -1 else len(text))

    def _find_line(self, index: int) -> int:
        # Binary search over starts
        lo, hi = 0, len(self.starts)
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if self.starts[mid] <= index:
                lo = mid
            else:
                hi = mid
        return lo

    def position(self, index: int) -> Dict[str, int]:
        if index < 0:
            index = 0
        if index > len(self.text):
            index = len(self.text)

        line = self._find_line(index)
        col_cp = index - self.starts[line]
        line_text = self.text[self.starts[line] : self.ends[line]]
        prefix = line_text[:col_cp]
        char_utf16 = len(prefix.encode("utf-16-le")) // 2
        return {"line": line, "character": char_utf16}

    def offset(self, line: int, character_utf16: int) -> int:
        """Convert an LSP (line, utf16-character) position to an absolute index.

        The LSP protocol measures ``character`` in UTF-16 code units.
        Python string indices are codepoints, so we need to convert.

        Best-effort clamping is applied if the position is out of bounds.
        """

        if line < 0:
            line = 0
        if line >= len(self.starts):
            line = len(self.starts) - 1

        if character_utf16 < 0:
            character_utf16 = 0

        line_start = self.starts[line]
        line_end = self.ends[line]
        line_text = self.text[line_start:line_end]

        units = 0
        cp = 0
        for ch in line_text:
            u = len(ch.encode("utf-16-le")) // 2
            if units + u > character_utf16:
                break
            units += u
            cp += 1

        return line_start + cp

    def range(self, span: Span) -> Dict[str, Any]:
        start, end = span
        return {"start": self.position(start), "end": self.position(end)}


def span_to_lsp_range(text: str, span: Span, index: Optional[TextIndex] = None) -> Dict[str, Any]:
    """Convert a span to an LSP range.

    ``index`` can be reused across conversions for performance.
    """

    idx = index or TextIndex(text)
    return idx.range(span)

