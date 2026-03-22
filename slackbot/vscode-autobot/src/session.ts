import type { CriticResult, IssueBundle } from "./types";

/** In-memory session for the active workspace window. */
export class PatchSession {
  issueNumber: number | undefined;
  /** When set (e.g. Slack deep link), overrides workspace `github.owner` / `github.repo`. */
  githubOwner: string | undefined;
  githubRepo: string | undefined;
  bundle: IssueBundle | undefined;
  plannerRaw: string | undefined;
  patcherRaw: string | undefined;
  lastCritic: CriticResult | undefined;

  reset(): void {
    this.issueNumber = undefined;
    this.githubOwner = undefined;
    this.githubRepo = undefined;
    this.bundle = undefined;
    this.plannerRaw = undefined;
    this.patcherRaw = undefined;
    this.lastCritic = undefined;
  }
}
