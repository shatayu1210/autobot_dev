import * as vscode from "vscode";
import { parseAutobotDeepLink } from "./deepLink";
import {
  copyOpenInEditorDeepLink,
  fetchIssue,
  openIssueFromDeepLink,
  openIssueInteractive,
  runCriticStep,
  runFullLoop,
  runPatcherStep,
  runPlannerStep,
  setGitHubToken,
  showRepoIndex,
} from "./orchestrator";
import { PatchTreeProvider } from "./patchTreeProvider";
import { PatchSession } from "./session";

let out: vscode.OutputChannel;
let session: PatchSession;
let tree: PatchTreeProvider;

export function activate(context: vscode.ExtensionContext): void {
  out = vscode.window.createOutputChannel("AutoBot Patcher");
  session = new PatchSession();
  tree = new PatchTreeProvider(session);

  const treeView = vscode.window.createTreeView("autobot.patchView", {
    treeDataProvider: tree,
  });
  context.subscriptions.push(treeView, out);

  const refresh = () => tree.refresh();

  context.subscriptions.push(
    vscode.window.registerUriHandler({
      handleUri: async (uri: vscode.Uri) => {
        if (uri.scheme !== vscode.env.uriScheme) {
          vscode.window.showErrorMessage(`AutoBot: unexpected URI scheme ${uri.scheme}`);
          return;
        }
        const parsed = parseAutobotDeepLink(uri);
        if (!parsed.ok) {
          vscode.window.showErrorMessage(`AutoBot deep link: ${parsed.message}`);
          return;
        }
        try {
          await openIssueFromDeepLink(session, parsed.value, context.secrets, out);
          refresh();
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          vscode.window.showErrorMessage(`AutoBot: ${msg}`);
        }
      },
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("autobot.openIssue", async () => {
      await openIssueInteractive(session);
      refresh();
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("autobot.fetchIssue", async () => {
      await fetchIssue(session, context.secrets, out);
      refresh();
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("autobot.runPlanner", async () => {
      await runPlannerStep(session, context.secrets, out);
      refresh();
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("autobot.runPatcher", async () => {
      await runPatcherStep(session, out);
      refresh();
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("autobot.runCritic", async () => {
      await runCriticStep(session, out);
      refresh();
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("autobot.runPatchLoop", async () => {
      await runFullLoop(session, context.secrets, out);
      refresh();
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("autobot.setGitHubToken", async () => {
      const token = await vscode.window.showInputBox({
        prompt: "GitHub personal access token (repo read for private)",
        password: true,
        ignoreFocusOut: true,
      });
      if (token) {
        await setGitHubToken(context.secrets, token);
        vscode.window.showInformationMessage("AutoBot: GitHub token stored in Secret Storage.");
      }
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("autobot.showRepoIndex", async () => {
      await showRepoIndex(out);
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("autobot.copyOpenLink", async () => {
      await copyOpenInEditorDeepLink(session);
    })
  );
}

export function deactivate(): void {
  // no-op
}
