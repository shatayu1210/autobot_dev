import * as vscode from "vscode";

/**
 * Must match `package.json`: `"publisher"."name"`.
 * Slack / web should link to: vscode://autobot.autobot-patcher/open?issue=NNN&owner=...&repo=...&fetch=1
 */
export const AUTOBOT_URI_AUTHORITY = "autobot.autobot-patcher";

export interface ParsedDeepLink {
  issue: number;
  owner?: string;
  repo?: string;
  /** When true, fetch issue from GitHub immediately after activation. */
  autoFetch: boolean;
}

export function parseAutobotDeepLink(
  uri: vscode.Uri
): { ok: true; value: ParsedDeepLink } | { ok: false; message: string } {
  // VS Code, Cursor, Insiders, etc. each use their own scheme; authority must match extension id.
  if (uri.authority !== AUTOBOT_URI_AUTHORITY) {
    return { ok: false, message: `Unexpected authority (expected ${AUTOBOT_URI_AUTHORITY})` };
  }
  const path = (uri.path || "").replace(/^\//, "") || "open";
  if (path !== "open") {
    return { ok: false, message: `Unknown path: ${path}` };
  }
  const q = new URLSearchParams(uri.query);
  const issueRaw = q.get("issue") ?? q.get("number") ?? "";
  if (!/^\d+$/.test(issueRaw)) {
    return { ok: false, message: "Missing or invalid query param: issue (numeric)" };
  }
  const owner = q.get("owner")?.trim() || undefined;
  const repo = q.get("repo")?.trim() || undefined;
  if ((owner && !repo) || (!owner && repo)) {
    return { ok: false, message: "Provide both owner and repo, or neither" };
  }
  const fetchParam = q.get("fetch");
  const autoFetch = fetchParam === "1" || fetchParam?.toLowerCase() === "true";
  return {
    ok: true,
    value: {
      issue: parseInt(issueRaw, 10),
      owner,
      repo,
      autoFetch,
    },
  };
}

/** Build a link you can paste into Slack (opens the host app that registered this extension). */
export function buildAutobotDeepLink(
  issue: number,
  options?: { owner?: string; repo?: string; autoFetch?: boolean; uriScheme?: string }
): string {
  const scheme = options?.uriScheme?.trim() || "vscode";
  const params = new URLSearchParams({ issue: String(issue) });
  if (options?.owner) {
    params.set("owner", options.owner);
  }
  if (options?.repo) {
    params.set("repo", options.repo);
  }
  if (options?.autoFetch) {
    params.set("fetch", "1");
  }
  return `${scheme}://${AUTOBOT_URI_AUTHORITY}/open?${params.toString()}`;
}
