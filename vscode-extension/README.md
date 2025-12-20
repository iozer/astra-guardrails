# Astra Guardrails (LSP) — VS Code Extension

This is a **thin VS Code client** for the Astra Guardrails language server.

It starts the server by spawning:

```bash
astra lsp
```

So this extension **does not** embed the Astra engine. It relies on an `astra` executable that must be available in the same environment as VS Code (WSL vs Windows).

Repo: https://github.com/iozer/astra-guardrails

---

## What you get

- Pinpoint diagnostics (schema / semantic / type / effect checks)
- Quick fixes (Code Actions) with **minimal text edits** (no full-file rewrites)
- Fix-all actions that are **prevalidated** (only offered if the result still parses & validates)

---

## Requirements

- VS Code `1.85+`
- Python `3.9+` (recommended `3.10+`)
- Astra installed (from the repo) so that `astra --help` works
- Node.js + npm (only required if you build the `.vsix` locally)

### WSL vs Windows (important)

- If you work in **WSL (Ubuntu)**: open the repo using **Remote - WSL** (bottom-left should show `WSL: Ubuntu`) and install the extension **in the WSL context**.
- If you work **natively on Windows**: install Astra in a Windows venv and install the extension locally on Windows.

If you mix contexts (e.g., VS Code in Windows but `astra` installed in WSL), the server won’t start and you won’t see diagnostics.

---

## 1) Install Astra (server) from this repo

From repo root (`astra-guardrails/`):

### WSL / Linux / macOS
```bash
python3 -m venv .venv
source .venv/bin/activate

python -m pip install -U pip
python -m pip install -e .

astra --help
command -v astra
```

### Windows PowerShell
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install -U pip
python -m pip install -e .

astra --help
where astra
```

> Ubuntu/Debian note: If you see `error: externally-managed-environment`, you tried to install into system Python.
> Use a venv (as above). Do **not** use `--break-system-packages`.

---

## 2) Build & package the extension (local VSIX)

From `vscode-extension/`:

```bash
npm install
npm run compile
npm run package
```

This produces a file like:

- `astra-guardrails-lsp-0.0.1.vsix`

> If `vsce` warns “LICENSE not found”, add a license file under `vscode-extension/` (e.g. copy `LICENSE-MIT` to `vscode-extension/LICENSE`).

---

## 3) Install the VSIX

### Option A — VS Code UI (recommended)
1. Open Extensions panel (`Ctrl+Shift+X`)
2. Click the “…” menu
3. **Install from VSIX…**
4. Select the generated `.vsix`

### Option B — CLI
```bash
code --install-extension astra-guardrails-lsp-0.0.1.vsix --force
```

> In WSL: run the `code ...` command **inside WSL**, and install the extension in the WSL window/context.

---

## 4) Configure `astra.serverCommand` (recommended)

Even if `astra` works in your terminal, VS Code may not inherit your venv PATH.
The most reliable setup is to point the extension to an **absolute** `astra` path.

VS Code → Settings → search **“Astra Guardrails”** → **Server Command**

Examples:

### WSL
```
/home/<user>/astra-guardrails/.venv/bin/astra
```

### Windows
```
C:\path\to\astra-guardrails\.venv\Scripts\astra.exe
```

---

## 5) First demo (recommended)

1) Open:
- `examples/guardrails/01_policy_broken.astra.json`

2) Confirm language mode:
- Bottom-right should show **Astra JSON**
- If not: `Ctrl+K` then `M` → select **Astra JSON**

3) Open Problems:
- View → **Problems**

You should see a diagnostic like `MissingEffect` (policy declares `pure` but calls `print`).

4) Apply Quick Fix:
- Put cursor on the error range
- Press `Ctrl + .`
- Apply “Add required effect …”

5) Verify from terminal:
```bash
astra effectcheck examples/guardrails/01_policy_broken.astra.json --json
```
Expected output: `[]` after the fix.

---

## Troubleshooting

### No diagnostics appear
Checklist:
- File name ends with `.astra.json`
- Language mode is **Astra JSON**
- Extension installed in the correct context (WSL vs Windows)
- `astra.serverCommand` points to a real executable
- Check logs:
  - View → Output → select an “Astra …” channel

### `spawn astra ENOENT` / “astra not found”
Set `astra.serverCommand` to an absolute path (see section 4).

### Activation error: `Cannot find module 'vscode-languageclient/node'`
Your VSIX is missing production dependencies at runtime.

Fix:
- Ensure `vscode-languageclient` is in `dependencies` (not `devDependencies`)
- Ensure `.vscodeignore` does **not** exclude `node_modules/**`
- Rebuild:
  ```bash
  rm -rf node_modules package-lock.json
  npm install
  npm run compile
  npm run package
  ```

### “Astra JSON” is not available in language list
The extension isn’t installed/active in this VS Code context.
Reinstall the VSIX in the correct environment (WSL window vs Windows window) and reload the window.

---

## License

See the repository root license files (`LICENSE-MIT`, `LICENSE-APACHE-2.0`) for licensing terms.
