# AutoBot Patcher — VS Code extension

Planner → Patcher → Critic orchestration for GitHub issues. Fetches issue data from the **GitHub REST API**, builds a **compact repo index** (Python `def` / `class` hints today; swap in Tree-sitter later), and calls your **HTTP gateway** in front of **Vertex AI**.

## Setup

1. Open this folder (`vscode-autobot`) in VS Code.
2. `npm install`
3. `npm run compile` (or `npm run watch` while developing)
4. **Run Extension** (F5) to launch a new Extension Development Host.

## Configuration

| Setting | Purpose |
|--------|---------|
| `autobot.github.owner` / `autobot.github.repo` | Target repo (default `apache` / `airflow`). |
| `autobot.inference.baseUrl` | Your API base URL (Cloud Run / gateway in front of Vertex). |
| `autobot.inference.apiKey` | Optional `Authorization: Bearer` for that gateway. |
| `autobot.inference.plannerPath` / `patcherPath` / `criticPath` | Paths appended to `baseUrl` (defaults `/v1/planner`, etc.). |

**GitHub token:** Command **AutoBot: Set GitHub Token** stores a PAT in VS Code Secret Storage (fine-grained `issues:read` or classic `public_repo` for public repos).

## Inference gateway contract

The extension `POST`s JSON and expects a response that includes model text in one of:

- `{ "text": "..." }`
- `{ "output": "..." }`
- `{ "prediction": "..." }`
- `{ "predictions": ["..."] }` (Vertex-style)

**Planner** body:

```json
{
  "issue_title": "",
  "issue_body": "",
  "labels": [],
  "assignee_logins": [],
  "comments_text": "",
  "repo_symbols_compact": ""
}
```

**Patcher** body:

```json
{
  "planner_output": "",
  "code_spans": ""
}
```

**Critic** body:

```json
{
  "issue_title": "",
  "issue_body": "",
  "planner_output": "",
  "proposed_diff": ""
}
```

Map these to your Vertex endpoints in the gateway.

## Slack / deep link (open issue in VS Code)

Install the packaged extension (or run F5 from source) so the URI handler is registered. Links use the **extension id** from `package.json` (`publisher.name`): authority **`autobot.autobot-patcher`**.

- In **VS Code** the scheme is usually `vscode://…`
- In **Cursor** the scheme is usually `cursor://…` (same authority and query)

Command **AutoBot: Copy Open-in-Editor Link** uses the running app’s `uriScheme` so Slack gets the right prefix.

| Query param | Required | Meaning |
|-------------|----------|---------|
| `issue` | yes | Issue number (digits only). Alias: `number`. |
| `owner`, `repo` | together optional | Override workspace `autobot.github.owner` / `autobot.github.repo` (e.g. `apache` / `airflow`). |
| `fetch` | optional | `1` or `true` — fetch issue from GitHub immediately (needs PAT). |

**Examples**

```text
vscode://autobot.autobot-patcher/open?issue=12345&owner=apache&repo=airflow&fetch=1
vscode://autobot.autobot-patcher/open?issue=12345
```

In Slack, add a button or markdown link with that URL. Users must have VS Code (or a compatible editor that registers the same handler) and this extension installed.

**Generate a link from VS Code:** command **AutoBot: Copy Open-in-Editor Link (for Slack)** — uses the active issue + current owner/repo settings and sets `fetch=1`.

## Commands

- **AutoBot: Open Issue by Number** — set active issue.
- **AutoBot: Fetch Issue from GitHub** — load issue, comments, linked PR hints.
- **AutoBot: Run Planner** / **Run Patcher** / **Run Critic** — single steps.
- **AutoBot: Run Planner → Patcher → Critic Loop** — full flow with revise loop.
- **AutoBot: Show Compact Repo Index** — debug `repo_symbols_compact`.
- **AutoBot: Copy Open-in-Editor Link (for Slack)** — `vscode://…` deep link with `fetch=1`.

Output goes to the **AutoBot Patcher** output channel; status is summarized in the **AutoBot Patch** explorer view.

## Packaging

```bash
npm install -g @vscode/vsce
vsce package
```
