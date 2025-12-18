\
# VS Code setup (Astra Guardrails)

## Option A (recommended): build and install the included local VSIX

```powershell
cd vscode-extension
npm install
npm run compile
npx @vscode/vsce package
```

Install the `.vsix`:
- VS Code → Extensions → “...” → Install from VSIX
or:
```powershell
code --install-extension .\astra-guardrails-lsp-0.0.1.vsix
```

### Windows/venv troubleshooting
If the extension cannot find `astra`, set:
- Settings → **Astra: Server Command** → `C:\path\to\venv\Scripts\astra.exe`


For packaging/publishing notes, see `vscode-extension/RELEASE.md`.
