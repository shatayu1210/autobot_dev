import type { GitHubComment, GitHubIssue, GitHubPullRef, IssueBundle } from "./types";

const API = "https://api.github.com";

export class GitHubRestClient {
  constructor(
    private readonly token: string,
    private readonly owner: string,
    private readonly repo: string
  ) {}

  private headers(): Record<string, string> {
    const h: Record<string, string> = {
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    };
    if (this.token) {
      h.Authorization = `Bearer ${this.token}`;
    }
    return h;
  }

  private async getJson<T>(path: string): Promise<T> {
    const url = `${API}/repos/${this.owner}/${this.repo}${path}`;
    const res = await fetch(url, { headers: this.headers() });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`GitHub ${res.status} ${path}: ${text.slice(0, 500)}`);
    }
    return res.json() as Promise<T>;
  }

  async getIssue(number: number): Promise<GitHubIssue> {
    return this.getJson<GitHubIssue>(`/issues/${number}`);
  }

  async listIssueComments(number: number): Promise<GitHubComment[]> {
    return this.getJson<GitHubComment[]>(`/issues/${number}/comments?per_page=100`);
  }

  /**
   * Linked PRs from timeline (cross-references). Falls back to empty if timeline unavailable.
   */
  async listLinkedPullNumbers(number: number): Promise<number[]> {
    type TimelineEvent = { event?: string; source?: { issue?: { number?: number } } };
    try {
      const url = `${API}/repos/${this.owner}/${this.repo}/issues/${number}/timeline?per_page=100`;
      const res = await fetch(url, {
        headers: {
          ...this.headers(),
          Accept: "application/vnd.github.mockingbird-preview+json",
        },
      });
      if (!res.ok) {
        return [];
      }
      const events = (await res.json()) as TimelineEvent[];
      const nums = new Set<number>();
      for (const e of events) {
        if (e.event === "cross-referenced" && e.source?.issue) {
          // PRs in GitHub are issues; we filter by fetching pull detail
          const n = e.source.issue.number;
          if (typeof n === "number") {
            nums.add(n);
          }
        }
      }
      return [...nums];
    } catch {
      return [];
    }
  }

  async getPull(number: number): Promise<GitHubPullRef | null> {
    try {
      const p = await this.getJson<{
        number: number;
        title: string;
        state: string;
        draft?: boolean;
        html_url: string;
      }>(`/pulls/${number}`);
      return {
        number: p.number,
        title: p.title,
        state: p.state,
        draft: p.draft,
        html_url: p.html_url,
      };
    } catch {
      return null;
    }
  }

  async fetchIssueBundle(number: number): Promise<IssueBundle> {
    const issue = await this.getIssue(number);
    const comments = await this.listIssueComments(number);
    const candidateNums = await this.listLinkedPullNumbers(number);
    const linkedPulls: GitHubPullRef[] = [];
    for (const n of candidateNums) {
      const pr = await this.getPull(n);
      if (pr) {
        linkedPulls.push(pr);
      }
    }
    return { issue, comments, linkedPulls };
  }
}
