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

        case "applyDiff": {
          // Write diff to a temp file and let the user apply it
          const diff = String(message.diff || "");
          const repoPath = String(message.repo_path || "");
          if (!diff) {
            vscode.window.showWarningMessage("AutoBot: No diff content to apply.");
            break;
          }
          const tmpUri = vscode.Uri.joinPath(
            this._context.globalStorageUri,
            "autobot_last.patch"
          );
          await vscode.workspace.fs.writeFile(tmpUri, Buffer.from(diff, "utf-8"));
          const action = await vscode.window.showInformationMessage(
            `AutoBot: Patch written to ${tmpUri.fsPath}. Apply it with: git apply ${tmpUri.fsPath}`,
            "Copy command",
            "Open patch file"
          );
          if (action === "Copy command") {
            await vscode.env.clipboard.writeText(
              `cd "${repoPath}" && git apply "${tmpUri.fsPath}"`
            );
            vscode.window.showInformationMessage("Command copied to clipboard.");
          } else if (action === "Open patch file") {
            await vscode.window.showTextDocument(tmpUri);
          }
          break;
        }

        case "openFile": {
          const repo = message.repo;
          const file = message.file;
          if (repo && file) {
            const uri = vscode.Uri.file(vscode.Uri.joinPath(vscode.Uri.file(repo), file).fsPath);
            vscode.window.showTextDocument(uri, { preview: true });
          }
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
    if (!this._view) return;
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
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'none';
             connect-src http://127.0.0.1:5000 http://localhost:5000;
             style-src ${webview.cspSource} 'unsafe-inline';
             script-src 'nonce-${nonce}';" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link href="${styleUri}" rel="stylesheet" />
  <title>AutoBot</title>
</head>
<body>

  <!-- Header -->
  <header id="ab-header">
    <span class="ab-logo">🤖</span>
    <span class="ab-title">AutoBot</span>
    <span class="ab-badge">Planner · Patcher · Critic</span>
  </header>

  <!-- Config: only repo path here; issue # goes in chat -->
  <div id="ab-config">
    <div class="ab-cfg-row">
      <label for="ab-repo">Repo</label>
      <input type="text" id="ab-repo" placeholder="Airflow repo path (auto-filled from workspace)" />
      <button class="ab-icon-btn" id="ab-pick" title="Browse for folder">…</button>
    </div>
  </div>

  <!-- Chat feed -->
  <div id="ab-chat">
    <div class="ab-welcome" id="ab-welcome">
      <div class="ab-welcome-icon">✦</div>
      <div>Ask AutoBot to fix an issue</div>
      <div style="font-size:0.8em;margin-top:4px;opacity:0.6;">e.g. <em>fix issue #45123</em></div>
    </div>
  </div>

  <!-- Input bar -->
  <div id="ab-input-bar">
    <textarea
      id="ab-input"
      rows="1"
      placeholder="Fix issue #45123 / check issue #100…  (Enter to send)"
    ></textarea>
    <button id="ab-send" title="Send">➤</button>
  </div>

  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }
}

function getNonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  return Array.from({ length: 32 }, () => chars[Math.floor(Math.random() * chars.length)]).join("");
}
