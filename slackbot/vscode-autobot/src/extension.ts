import * as vscode from "vscode";
import {
  fetchIssue,
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
}

export function deactivate(): void {
  // no-op
}
