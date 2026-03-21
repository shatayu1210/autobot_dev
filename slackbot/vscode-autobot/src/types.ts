/** GitHub issue (subset used by orchestrator). */
export interface GitHubIssue {
  number: number;
  title: string;
  body: string | null;
  state: string;
  labels: { name: string }[];
  assignees: { login: string }[];
  html_url: string;
}

export interface GitHubComment {
  user: { login: string };
  body: string | null;
  created_at: string;
}

export interface GitHubPullRef {
  number: number;
  title: string;
  state: string;
  draft?: boolean;
  html_url: string;
}

export interface IssueBundle {
  issue: GitHubIssue;
  comments: GitHubComment[];
  linkedPulls: GitHubPullRef[];
}

export type CriticVerdict = "ACCEPT" | "REVISE" | "REJECT";

export interface PlannerResult {
  raw: string;
  requiresCodeChange: boolean | null;
  confidence: "HIGH" | "MEDIUM" | "LOW" | null;
  reason: string | null;
  planBody: string | null;
}

export interface CriticResult {
  raw: string;
  verdict: CriticVerdict | null;
  reasoning: string;
}

export interface InferenceRequestPlanner {
  issue_title: string;
  issue_body: string;
  labels: string[];
  assignee_logins: string[];
  comments_text: string;
  repo_symbols_compact: string;
}

export interface InferenceRequestPatcher {
  planner_output: string;
  code_spans: string;
}

export interface InferenceRequestCritic {
  issue_title: string;
  issue_body: string;
  planner_output: string;
  proposed_diff: string;
}
