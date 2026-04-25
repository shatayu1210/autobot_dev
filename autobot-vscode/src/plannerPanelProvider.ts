import * as vscode from "vscode";
import { orchestrate, OrchestratePayload } from "./orchestratorClient";

export class PlannerPanelProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "autobot.plannerPanel";

  private _view?: vscode.WebviewView;

  constructor(private readonly _context: vscode.ExtensionContext) {}

  resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ): void {
    this._view = webviewView;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this._context.extensionUri, "media")],
    };

    webviewView.webview.html = this._getHtml(webviewView.webview);

    webviewView.webview.onDidReceiveMessage(async (message) => {
      switch (message.type) {
        case "ready":
          this._postConfig();
          break;
        case "orchestrate": {
          const payload = message.payload as OrchestratePayload;
          try {
            const result = await orchestrate(this._context.secrets, payload);
            webviewView.webview.postMessage({
              type: "orchestrateResult",
              id: message.id,
              ok: true,
              result,
            });
          } catch (e) {
            const err = e instanceof Error ? e.message : String(e);
            webviewView.webview.postMessage({
              type: "orchestrateResult",
              id: message.id,
              ok: false,
              error: err,
            });
          }
          break;
        }
        case "pickFolder": {
          const picked = await vscode.window.showOpenDialog({
            canSelectFiles: false,
            canSelectFolders: true,
            canSelectMany: false,
            openLabel: "Select repository root",
          });
          const folder = picked?.[0]?.fsPath;
          webviewView.webview.postMessage({
            type: "folderPicked",
            folder: folder ?? null,
          });
          break;
        }
        default:
          break;
      }
    });

    this._context.subscriptions.push(
      vscode.workspace.onDidChangeConfiguration(() => this._postConfig()),
      vscode.workspace.onDidChangeWorkspaceFolders(() => this._postConfig())
    );
  }

  private _postConfig(): void {
    if (!this._view) {
      return;
    }
    const ws = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? "";
    const config = vscode.workspace.getConfiguration("autobot");
    this._view.webview.postMessage({
      type: "config",
      defaultRepoPath: ws,
      serverUrl: config.get<string>("serverUrl"),
      orchestratePath: config.get<string>("orchestratePath"),
    });
  }

  private _getHtml(webview: vscode.Webview): string {
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._context.extensionUri, "media", "webview.js")
    );
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._context.extensionUri, "media", "webview.css")
    );
    const nonce = getNonce();

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link href="${styleUri}" rel="stylesheet" />
  <title>AutoBot</title>
</head>
<body>
  <header class="ab-header">
    <div class="ab-title">Planner → Patcher → Critic</div>
    <p class="ab-sub">POSTs to your orchestrator (<code>autobot.serverUrl</code> + <code>autobot.orchestratePath</code>). Use <strong>Set API bearer token</strong> for IAP/Cloud Run.</p>
  </header>

  <section class="ab-section">
    <label>Issue #</label>
    <input type="text" id="issueNumber" placeholder="e.g. 124" autocomplete="off" />
  </section>

  <section class="ab-section">
    <label>Repository path (local clone)</label>
    <div class="ab-row">
      <input type="text" id="repoPath" placeholder="Defaults to first workspace folder" />
      <button type="button" id="pickFolder" title="Choose folder">…</button>
    </div>
  </section>

  <section class="ab-actions">
    <button type="button" id="btnAsk" class="ab-btn primary">1 · Load issue</button>
    <button type="button" id="btnPlan" class="ab-btn primary">2 · Planner</button>
    <button type="button" id="btnPatch" class="ab-btn primary">3 · Patcher + Critic</button>
    <button type="button" id="btnOpenPr" class="ab-btn primary">4 · Open PR draft</button>
  </section>

  <section class="ab-section ab-status">
    <span id="status" class="muted">Idle</span>
  </section>

  <section class="ab-section">
    <label>Issue / plan context (last response)</label>
    <pre id="contextOut" class="ab-pre"></pre>
  </section>

  <section class="ab-section">
    <label>Diff / verdict (last patch step)</label>
    <pre id="patchOut" class="ab-pre"></pre>
  </section>

  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }
}

function getNonce(): string {
  let text = "";
  const possible = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  for (let i = 0; i < 32; i++) {
    text += possible.charAt(Math.floor(Math.random() * possible.length));
  }
  return text;
}
