import * as vscode from "vscode";

export type OrchestrateCommand =
  | "ask_issue"
  | "plan_patch"
  | "accept_plan"
  | "open_pr";

export interface OrchestratePayload {
  command: OrchestrateCommand;
  issue_number?: number;
  repo_path?: string;
  plan?: unknown;
  code_spans?: unknown;
  diff?: string;
  [key: string]: unknown;
}

const SECRET_KEY = "autobot.apiBearerToken";

export async function getStoredToken(
  secrets: vscode.SecretStorage
): Promise<string | undefined> {
  return secrets.get(SECRET_KEY);
}

export async function setStoredToken(
  secrets: vscode.SecretStorage,
  token: string | undefined
): Promise<void> {
  if (token === undefined || token === "") {
    await secrets.delete(SECRET_KEY);
    return;
  }
  await secrets.store(SECRET_KEY, token);
}

export async function orchestrate(
  secrets: vscode.SecretStorage,
  payload: OrchestratePayload
): Promise<unknown> {
  const config = vscode.workspace.getConfiguration("autobot");
  const baseUrl = String(config.get<string>("serverUrl") ?? "http://localhost:5000").replace(
    /\/$/,
    ""
  );
  const path = String(config.get<string>("orchestratePath") ?? "/api/orchestrate");
  const timeoutMs = Number(config.get<number>("requestTimeoutMs") ?? 300000);
  const url = `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;

  const token = await getStoredToken(secrets);
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    throw new Error(`Request failed (${url}): ${msg}`);
  } finally {
    clearTimeout(timer);
  }

  const text = await response.text();
  let body: unknown = text;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }

  if (!response.ok) {
    const detail =
      typeof body === "object" && body !== null && "error" in body
        ? JSON.stringify(body)
        : typeof body === "string"
          ? body
          : response.statusText;
    throw new Error(`HTTP ${response.status}: ${detail}`);
  }

  return body;
}
