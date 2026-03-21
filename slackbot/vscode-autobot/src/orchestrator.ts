import * as vscode from "vscode";
import { getGitHubOwnerRepo, getInferenceConfig, getMaxCriticIterations, getRepoIndexConfig } from "./config";
import { GitHubRestClient } from "./githubRest";
import { InferenceClient } from "./inferenceClient";
import { parseCriticOutput, parsePlannerOutput } from "./parsers";
import {
  buildCompactRepoIndex,
  extractCodeSpansForPlan,
  formatRepoSymbolsCompact,
  getWorkspaceRoot,
} from "./repoIndex";
import type { PatchSession } from "./session";

const SECRET_KEY = "autobot.githubPat";
const SYMBOLS_MAX_CHARS = 12_000;

export async function ensureGitHubToken(secrets: vscode.SecretStorage): Promise<string> {
  const t = await secrets.get(SECRET_KEY);
  if (!t) {
    throw new Error(
      "No GitHub token. Run command “AutoBot: Set GitHub Token” (repo read scope is enough for public repos)."
    );
  }
  return t;
}

export async function setGitHubToken(secrets: vscode.SecretStorage, token: string): Promise<void> {
  await secrets.store(SECRET_KEY, token.trim());
}

export async function openIssueInteractive(session: PatchSession): Promise<void> {
  const input = await vscode.window.showInputBox({
    prompt: "GitHub issue number",
    validateInput: (v) => (/^\d+$/.test(v.trim()) ? null : "Enter a numeric issue number"),
  });
  if (!input) {
    return;
  }
  session.issueNumber = parseInt(input.trim(), 10);
  session.bundle = undefined;
  session.plannerRaw = undefined;
  session.patcherRaw = undefined;
  session.lastCritic = undefined;
  vscode.window.showInformationMessage(`AutoBot: active issue #${session.issueNumber}`);
}

export async function fetchIssue(
  session: PatchSession,
  secrets: vscode.SecretStorage,
  out: vscode.OutputChannel
): Promise<void> {
  if (!session.issueNumber) {
    await openIssueInteractive(session);
  }
  if (!session.issueNumber) {
    return;
  }
  const token = await ensureGitHubToken(secrets);
  const { owner, repo } = getGitHubOwnerRepo();
  const gh = new GitHubRestClient(token, owner, repo);
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Fetching issue #${session.issueNumber}` },
    async () => {
      session.bundle = await gh.fetchIssueBundle(session.issueNumber!);
    }
  );
  const b = session.bundle;
  if (!b) {
    return;
  }
  out.appendLine(`--- Issue #${b.issue.number} ${b.issue.title}`);
  out.appendLine(b.issue.html_url);
  out.appendLine(`Comments: ${b.comments.length}, linked PRs: ${b.linkedPulls.length}`);
  out.show(true);
  vscode.window.showInformationMessage(`Fetched issue #${session.issueNumber}`);
}

function commentsBlock(bundle: import("./types").IssueBundle): string {
  return bundle.comments
    .map((c) => `[${c.user.login} ${c.created_at}] ${(c.body || "").slice(0, 2000)}`)
    .join("\n\n");
}

export async function runPlannerStep(
  session: PatchSession,
  secrets: vscode.SecretStorage,
  out: vscode.OutputChannel
): Promise<void> {
  if (!session.bundle) {
    vscode.window.showWarningMessage("Fetch an issue first.");
    return;
  }
  const root = await getWorkspaceRoot();
  if (!root) {
    vscode.window.showWarningMessage("Open a folder/workspace for repo index.");
    return;
  }
  const indexCfg = getRepoIndexConfig();
  const entries = buildCompactRepoIndex(root, indexCfg);
  const symbols = formatRepoSymbolsCompact(entries, SYMBOLS_MAX_CHARS);
  const b = session.bundle;
  const client = new InferenceClient(getInferenceConfig());
  const req = {
    issue_title: b.issue.title,
    issue_body: b.issue.body || "",
    labels: b.issue.labels.map((l) => l.name),
    assignee_logins: b.issue.assignees.map((a) => a.login),
    comments_text: commentsBlock(b),
    repo_symbols_compact: symbols,
  };
  out.appendLine("--- Planner request (repo_symbols_compact truncated in UI if huge) …");
  let raw: string;
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "AutoBot: Planner" },
    async () => {
      raw = await client.planner(req);
    }
  );
  session.plannerRaw = raw!;
  const parsed = parsePlannerOutput(raw!);
  out.appendLine("--- Planner output ---");
  out.appendLine(raw!);
  out.appendLine(`--- Parsed requires_code_change=${parsed.requiresCodeChange} confidence=${parsed.confidence}`);
  out.show(true);
}

export async function runPatcherStep(
  session: PatchSession,
  out: vscode.OutputChannel
): Promise<void> {
  if (!session.plannerRaw) {
    vscode.window.showWarningMessage("Run Planner first.");
    return;
  }
  const root = await getWorkspaceRoot();
  if (!root) {
    vscode.window.showWarningMessage("Open a folder/workspace.");
    return;
  }
  const indexCfg = getRepoIndexConfig();
  const entries = buildCompactRepoIndex(root, indexCfg);
  const codeSpans = extractCodeSpansForPlan(root, session.plannerRaw, entries, 8000);
  const client = new InferenceClient(getInferenceConfig());
  let raw: string;
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "AutoBot: Patcher" },
    async () => {
      raw = await client.patcher({
        planner_output: session.plannerRaw!,
        code_spans: codeSpans,
      });
    }
  );
  session.patcherRaw = raw!;
  out.appendLine("--- Patcher output (diff) ---");
  out.appendLine(raw!);
  out.show(true);
}

export async function runCriticStep(
  session: PatchSession,
  out: vscode.OutputChannel
): Promise<void> {
  if (!session.bundle || !session.plannerRaw || !session.patcherRaw) {
    vscode.window.showWarningMessage("Need issue, planner, and patcher output.");
    return;
  }
  const b = session.bundle;
  const client = new InferenceClient(getInferenceConfig());
  let raw: string;
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "AutoBot: Critic" },
    async () => {
      raw = await client.critic({
        issue_title: b.issue.title,
        issue_body: b.issue.body || "",
        planner_output: session.plannerRaw!,
        proposed_diff: session.patcherRaw!,
      });
    }
  );
  session.lastCritic = parseCriticOutput(raw!);
  out.appendLine("--- Critic output ---");
  out.appendLine(raw!);
  out.appendLine(`--- Verdict: ${session.lastCritic.verdict}`);
  out.show(true);
}

export async function runFullLoop(
  session: PatchSession,
  secrets: vscode.SecretStorage,
  out: vscode.OutputChannel
): Promise<void> {
  if (!session.bundle) {
    await fetchIssue(session, secrets, out);
  }
  if (!session.bundle) {
    return;
  }
  await runPlannerStep(session, secrets, out);
  const parsed = parsePlannerOutput(session.plannerRaw || "");
  if (parsed.requiresCodeChange === false) {
    const go = await vscode.window.showInformationMessage(
      `Planner: no code change (${parsed.reason || "n/a"}). Run patch flow anyway?`,
      "Proceed",
      "Stop"
    );
    if (go !== "Proceed") {
      return;
    }
  } else if (parsed.requiresCodeChange === true && parsed.confidence === "MEDIUM") {
    const ok = await vscode.window.showWarningMessage(
      "Planner confidence MEDIUM. Continue to Patcher?",
      "Continue",
      "Cancel"
    );
    if (ok !== "Continue") {
      return;
    }
  }
  const maxIter = getMaxCriticIterations();
  for (let i = 0; i < maxIter; i++) {
    await runPatcherStep(session, out);
    await runCriticStep(session, out);
    const v = session.lastCritic?.verdict;
    if (v === "ACCEPT" || v === "REJECT") {
      break;
    }
    if (v === "REVISE" && i < maxIter - 1) {
      const feedback = session.lastCritic?.reasoning || "";
      session.plannerRaw = `${session.plannerRaw}\n\nCRITIC_FEEDBACK:\n${feedback}`;
    }
  }
  vscode.window.showInformationMessage("AutoBot: patch loop finished (see output channel).");
}

export async function showRepoIndex(out: vscode.OutputChannel): Promise<void> {
  const root = await getWorkspaceRoot();
  if (!root) {
    vscode.window.showWarningMessage("Open a folder/workspace.");
    return;
  }
  const entries = buildCompactRepoIndex(root, getRepoIndexConfig());
  const text = formatRepoSymbolsCompact(entries, 50_000);
  out.appendLine("--- Compact repo index ---");
  out.appendLine(text);
  out.show(true);
}
