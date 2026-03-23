import * as vscode from "vscode";
import { PlannerPanelProvider } from "./plannerPanelProvider";
import { setStoredToken } from "./orchestratorClient";

export function activate(context: vscode.ExtensionContext): void {
  const provider = new PlannerPanelProvider(context);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(PlannerPanelProvider.viewType, provider)
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("autobot.setApiToken", async () => {
      const token = await vscode.window.showInputBox({
        title: "AutoBot API bearer token",
        prompt: "Stored in VS Code Secret Storage (not workspace). Leave empty to clear.",
        password: true,
        ignoreFocusOut: true,
      });
      if (token === undefined) {
        return;
      }
      await setStoredToken(context.secrets, token.trim() === "" ? undefined : token.trim());
      vscode.window.showInformationMessage(
        token.trim() === ""
          ? "AutoBot API token cleared."
          : "AutoBot API token saved."
      );
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("autobot.clearApiToken", async () => {
      await setStoredToken(context.secrets, undefined);
      vscode.window.showInformationMessage("AutoBot API token cleared.");
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("autobot.openPanel", async () => {
      await vscode.commands.executeCommand("autobot.plannerPanel.focus");
    })
  );
}

export function deactivate(): void {}
