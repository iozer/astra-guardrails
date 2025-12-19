# Astra Guardrails — Verifiable Guardrails for LLM/ML Systems

**Astra Guardrails** (`astra-guardrails`) is an **LLM-first policy and verification layer** for production systems.

It is designed for the class of logic that:
- changes frequently,
- has security or operational consequences,
- must be explainable,
- and must be safe by default.

Astra is **not** a general-purpose language. It is a **small, verifiable policy surface** you place **between** a model/LLM and privileged actions (tools, network calls, config changes).

Repo: https://github.com/iozer/astra-guardrails  
Author: https://www.linkedin.com/in/ilyas-%C3%B6zer-192a60a1  
Contact: iozer@bandirma.edu.tr

---

## Who this is for

- **Security teams:** least-privilege capability boundaries for tool use, explicit effect gating, auditable decisions.
- **Platform teams:** policy-as-code that is versioned, testable, reproducible, and easy to integrate (drop-in patterns).
- **Product / LLM teams:** fast iteration on agent/tool policies with minimal diffs, editor quick-fixes, and structured reasons.

---

## Why Astra Guardrails

Most teams shipping LLM features converge on the same operational gap:

> A model can propose actions, but it cannot be the final authority.

Astra separates **generation** from **authority**:
- LLMs generate proposals (plans, tool calls, patches).
- Astra verifies structure + semantics + types + declared capabilities.
- Your host application executes only what is allowed, and logs structured reasons.

---

## What you can build (concrete examples)

### 1) LLM tool-use guardrails (primary)
**Scenario:** an LLM proposes tool calls (`send_email`, `http_get`, `db_write`, …).  
**Astra role:** decide **ALLOW / DENY / REVIEW** with explicit reasons and enforce capability limits.

Typical rules:
- tool allowlist / denylist
- parameter constraints (e.g., “only company domains”, “no external URLs”)
- budget gates (tokens, request counts, time windows)
- escalation rules (“human approval when risk is high”)

### 2) ML output safety / QA gates
**Scenario:** a model outputs a score/confidence that triggers an action (refund, block, route).  
**Astra role:** gate and override outputs when constraints apply (auto / manual review / block) with reasons.

### 3) Safe change application (LLM-generated config / patch gating)
**Scenario:** an LLM produces a config change or policy update.  
**Astra role:** enforce invariants and governance rules before applying changes (idempotence, capability expansion, schema/version constraints).

---

## Core guarantees

### Validation pipeline
Astra modules run through:
- **Schema validation** (strict JSON structure)
- **Semantic checks** (undefined variables, missing returns, unreachable statements)
- **Type checks** (records/lists, basic generics where applicable)
- **Effect checks** (capabilities required by calls must be declared)

### Capability / effect gating
Functions declare effects, for example:
- `["pure"]`
- `["io.print"]`
- `["net.http"]`

If a call graph requires an effect not declared, validation fails.

> Important: Astra is a verification/capability layer. It is **not** a stand-alone sandbox for hostile code.

### Developer experience (LSP)
Astra includes an LSP server that provides:
- pinpoint diagnostics (JSON Pointer → exact token range)
- quick fixes with **minimal edits** (no full-file rewrites)
- prevalidated code actions (parse + schema + no-regression + target-diagnostic disappears)
- fix-all with idempotence guarantees

---

## Quickstart (copy‑paste runnable)

> Requirements:
> - Python 3.10+
> - `astra` CLI on PATH (install the Astra toolkit repo or `pip install -e .` in the Astra repo)

### 1) Format a demo policy
```bash
astra format examples/guardrails/01_policy_broken.astra.json --in-place
```

### 2) Show effect-gating errors
```bash
astra effectcheck examples/guardrails/01_policy_broken.astra.json --json
```

### 3) VS Code + LSP quick fix (recommended)
Start the LSP server:
```bash
astra lsp
```

Then open:
- `examples/guardrails/01_policy_broken.astra.json`

You should see a diagnostic: the policy declares `pure` but calls `print`.
Apply the quick fix; note the **minimal diff** (only the `effects` token changes).

### 4) Run tests (after quick fix)
```bash
astra test examples/guardrails/01_policy_broken.astra.json --json
```

### 5) Execute a policy function (AST sandbox)
```bash
astra run-ast examples/guardrails/02_policy_ok.astra.json --fn decide_tool --args "http_get" "example.com" 50
```

Expected output is a record with `{decision, reason}`.

---

## VS Code integration

See `VSCODE_SETUP.md` for:
- building and installing the local `.vsix` extension (recommended)
- troubleshooting PATH/venv issues on Windows

---


---

## License

This project is dual-licensed under **MIT OR Apache-2.0** — you may choose either license at your option.

See `LICENSE-MIT` and `LICENSE-APACHE-2.0`.
