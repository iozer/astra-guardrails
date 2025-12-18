# VS Code Extension — VSIX Build & Marketplace Release Notes

This folder contains a thin VS Code client for the Astra language server.

Repository: https://github.com/iozer/astra-guardrails

---

## 1) Local VSIX build (recommended first step)

Prerequisites:
- Node.js (>= 18, recommended 20)
- npm
- VS Code

From the repo root:

```bash
cd vscode-extension
npm install
npm run compile
npx @vscode/vsce package
```

This produces a `.vsix` file in `vscode-extension/`.

Install locally:
- VS Code → Extensions → “…” → Install from VSIX
or
```bash
code --install-extension ./vscode-extension/astra-guardrails-lsp-0.0.1.vsix
```

---

## 2) Marketplace readiness checklist

### Metadata
Ensure `package.json` has (minimum):
- `name`, `displayName`, `description`
- `version` (SemVer)
- `publisher` (must match your Marketplace publisher ID)
- `repository`, `bugs`, `homepage`
- `icon` (recommended)
- `engines.vscode`

### Publisher
You must create a **Publisher** in the Visual Studio Marketplace and use that ID as `"publisher"`.
Currently set to: `"publisher": "iozer"`

### Token
To publish from CLI, create a Marketplace Personal Access Token (PAT) and store it securely.
You can publish with:
- environment variable `VSCE_PAT`
- or interactive login (depending on tooling)

---

## 3) Publish (CLI)

Install the publisher tool:
```bash
npm install -g @vscode/vsce
```

Then publish:
```bash
cd vscode-extension
vsce publish
```

Or publish a specific version:
```bash
vsce publish 0.0.2
```

---

## 4) Release process suggestion

- Update `CHANGELOG.md`
- Bump `version` in `package.json`
- Tag a GitHub release
- Use CI workflow `.github/workflows/vscode_vsix.yml` to build a VSIX artifact on release
- Attach the VSIX to the GitHub release for users who prefer manual installation


### GitHub release automation
If you create a GitHub Release, `.github/workflows/vscode_vsix.yml` will build a `.vsix` and attach it to the release automatically.
