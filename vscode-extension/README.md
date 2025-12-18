# Astra Guardrails — VS Code Extension (Thin LSP Client)

This extension starts the Astra language server by spawning:

    astra lsp

Repo: https://github.com/iozer/astra-guardrails

## Build & install (local)
```bash
cd vscode-extension
npm install
npm run compile
npx @vscode/vsce package
```

Install the `.vsix`:
- VS Code → Extensions → “…” → Install from VSIX
or:
```bash
code --install-extension astra-guardrails-lsp-0.0.1.vsix
```

## Setting (Windows/venv)
If VS Code cannot find `astra`, set:
- Settings → `astra.serverCommand` → `C:\path\to\venv\Scripts\astra.exe`
