"""A small "drop-in" integration layer for Astra.

This is intentionally lightweight: it wires together the existing validators and sandboxes
into a single ergonomic API for Python hosts.

Typical usage:

    from astra.engine import AstraEngine

    eng = AstraEngine(allowed_effects=["pure"])
    mod = eng.load_path("rules/my.astra.json")
    diag = eng.diagnose(mod)
    if diag.any_errors:
        ...
    result = eng.run(mod, "my_fn", [123])

"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import json

from astra.tools import fmt
from astra.tools import semantic
from astra.tools import typecheck
from astra.tools import effects
from astra.tools import sandbox_ast
from astra.tools import sandbox_exec_py


@dataclass
class Diagnostics:
    schema: List[Dict[str, Any]]
    semantic: List[Dict[str, Any]]
    types: List[Dict[str, Any]]
    effects: List[Dict[str, Any]]

    @property
    def any_errors(self) -> bool:
        # Today we don't model severity consistently across checkers; treat non-empty as errors.
        return bool(self.schema or self.semantic or self.types or self.effects)


class AstraEngine:
    def __init__(self, *, allowed_effects: Optional[List[str]] = None, mode: str = "ast"):
        self.allowed_effects = allowed_effects or ["pure"]
        if mode not in {"ast", "py"}:
            raise ValueError("mode must be 'ast' or 'py'")
        self.mode = mode
        self._schema = fmt.load_schema()

    def load_path(self, path: str | Path) -> Dict[str, Any]:
        p = Path(path)
        return json.loads(p.read_text(encoding="utf-8"))

    def format(self, module: Dict[str, Any]) -> Dict[str, Any]:
        return fmt.canonicalize(module)

    def diagnose(self, module: Dict[str, Any]) -> Diagnostics:
        schema_issues = fmt.validate(module, self._schema)
        sem_issues = semantic.check_module(module)
        type_issues = typecheck.check_module(module)
        eff_issues = effects.check_module(module)
        return Diagnostics(schema=schema_issues, semantic=sem_issues, types=type_issues, effects=eff_issues)

    def run(self, module: Dict[str, Any], fn: str, args: List[Any]) -> Any:
        # NOTE: For production, you typically want to call diagnose() first.
        if self.mode == "ast":
            return sandbox_ast.run_module(module, fn, args, self.allowed_effects)
        return sandbox_exec_py.run_python_sandbox(module, fn, args, self.allowed_effects)
