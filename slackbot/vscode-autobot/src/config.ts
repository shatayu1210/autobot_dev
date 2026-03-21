import * as vscode from "vscode";

const SECTION = "autobot";

export interface AutobotInferenceConfig {
  baseUrl: string;
  apiKey: string;
  plannerPath: string;
  patcherPath: string;
  criticPath: string;
  timeoutMs: number;
}

export interface AutobotRepoIndexConfig {
  maxFiles: number;
  maxDepth: number;
}

export function getGitHubOwnerRepo(): { owner: string; repo: string } {
  const c = vscode.workspace.getConfiguration(SECTION);
  return {
    owner: c.get<string>("github.owner", "apache"),
    repo: c.get<string>("github.repo", "airflow"),
  };
}

export function getInferenceConfig(): AutobotInferenceConfig {
  const c = vscode.workspace.getConfiguration(SECTION);
  return {
    baseUrl: (c.get<string>("inference.baseUrl", "") || "").replace(/\/$/, ""),
    apiKey: c.get<string>("inference.apiKey", "") || "",
    plannerPath: c.get<string>("inference.plannerPath", "/v1/planner"),
    patcherPath: c.get<string>("inference.patcherPath", "/v1/patcher"),
    criticPath: c.get<string>("inference.criticPath", "/v1/critic"),
    timeoutMs: c.get<number>("inference.timeoutMs", 120_000),
  };
}

export function getMaxCriticIterations(): number {
  return vscode.workspace.getConfiguration(SECTION).get<number>("patch.maxCriticIterations", 3);
}

export function getRepoIndexConfig(): AutobotRepoIndexConfig {
  const c = vscode.workspace.getConfiguration(SECTION);
  return {
    maxFiles: c.get<number>("repoIndex.maxFiles", 400),
    maxDepth: c.get<number>("repoIndex.maxDepth", 8),
  };
}
