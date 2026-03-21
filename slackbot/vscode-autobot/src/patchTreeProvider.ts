import * as vscode from "vscode";
import type { PatchSession } from "./session";

/**
 * Minimal sidebar tree: active issue and step status.
 */
export class PatchTreeProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private readonly _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;

  constructor(private readonly session: PatchSession) {}

  refresh(): void {
    this._onDidChange.fire();
  }

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  async getChildren(): Promise<vscode.TreeItem[]> {
    const items: vscode.TreeItem[] = [];
    const issue = new vscode.TreeItem(
      this.session.issueNumber ? `Issue #${this.session.issueNumber}` : "No issue selected",
      vscode.TreeItemCollapsibleState.None
    );
    issue.iconPath = new vscode.ThemeIcon("issue-opened");
    items.push(issue);

    const fetch = new vscode.TreeItem(
      this.session.bundle ? "Issue data: loaded" : "Issue data: not fetched",
      vscode.TreeItemCollapsibleState.None
    );
    fetch.iconPath = new vscode.ThemeIcon("cloud-download");
    items.push(fetch);

    const plan = new vscode.TreeItem(
      this.session.plannerRaw ? "Planner: done" : "Planner: pending",
      vscode.TreeItemCollapsibleState.None
    );
    plan.iconPath = new vscode.ThemeIcon("list-tree");
    items.push(plan);

    const patch = new vscode.TreeItem(
      this.session.patcherRaw ? "Patcher: done" : "Patcher: pending",
      vscode.TreeItemCollapsibleState.None
    );
    patch.iconPath = new vscode.ThemeIcon("diff");
    items.push(patch);

    const crit = new vscode.TreeItem(
      this.session.lastCritic?.verdict
        ? `Critic: ${this.session.lastCritic.verdict}`
        : "Critic: pending",
      vscode.TreeItemCollapsibleState.None
    );
    crit.iconPath = new vscode.ThemeIcon("comment-discussion");
    items.push(crit);

    return items;
  }
}
