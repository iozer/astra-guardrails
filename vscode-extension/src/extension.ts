import * as vscode from 'vscode';
import { LanguageClient, LanguageClientOptions, ServerOptions } from 'vscode-languageclient/node';

let client: LanguageClient | undefined;

export async function activate(context: vscode.ExtensionContext) {
  const config = vscode.workspace.getConfiguration('astra');
  const serverCommand = config.get<string>('serverCommand', 'astra');

  const serverOptions: ServerOptions = {
    command: serverCommand,
    args: ['lsp'],
    options: { env: process.env }
  };

  const clientOptions: LanguageClientOptions = {
    documentSelector: [{ language: 'astra-json' }],
    synchronize: {
      fileEvents: vscode.workspace.createFileSystemWatcher('**/*.astra.json')
    }
  };

  client = new LanguageClient(
    'astraGuardrailsLsp',
    'Astra Guardrails Language Server',
    serverOptions,
    clientOptions
  );

  client.start();
  context.subscriptions.push({ dispose: () => { void client?.stop(); } });

}

export async function deactivate(): Promise<void> {
  if (client) await client.stop();
}
