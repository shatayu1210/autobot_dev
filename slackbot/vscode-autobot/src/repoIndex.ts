import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import type { AutobotRepoIndexConfig } from "./config";

const SKIP_DIR = new Set([
  "node_modules",
  ".git",
  ".venv",
  "venv",
  "dist",
  "out",
  "build",
  "__pycache__",
  ".mypy_cache",
  ".tox",
  "target",
]);

const CODE_EXT = new Set([
  ".py",
  ".ts",
  ".tsx",
  ".js",
  ".jsx",
  ".go",
  ".rs",
  ".java",
  ".kt",
  ".scala",
  ".c",
  ".h",
  ".cpp",
  ".hpp",
  ".sql",
  ".yaml",
  ".yml",
  ".json",
  ".md",
]);

export interface RepoIndexEntry {
  relPath: string;
  /** Rough symbol hints (regex for Python defs); replace with Tree-sitter later. */
  symbols: string[];
  lineCount: number;
}

function isIgnored(rel: string): boolean {
  const parts = rel.split(path.sep);
  return parts.some((p) => p.startsWith(".") && p !== ".github");
}

function walk(
  root: string,
  dir: string,
  depth: number,
  maxDepth: number,
  maxFiles: number,
  out: RepoIndexEntry[]
): void {
  if (out.length >= maxFiles || depth > maxDepth) {
    return;
  }
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  entries.sort((a, b) => a.name.localeCompare(b.name));
  for (const ent of entries) {
    if (out.length >= maxFiles) {
      break;
    }
    if (SKIP_DIR.has(ent.name)) {
      continue;
    }
    const full = path.join(dir, ent.name);
    const rel = path.relative(root, full);
    if (isIgnored(rel)) {
      continue;
    }
    if (ent.isDirectory()) {
      walk(root, full, depth + 1, maxDepth, maxFiles, out);
    } else if (ent.isFile()) {
      const ext = path.extname(ent.name).toLowerCase();
      if (!CODE_EXT.has(ext)) {
        continue;
      }
      let text = "";
      try {
        text = fs.readFileSync(full, "utf8");
      } catch {
        continue;
      }
      const lines = text.split(/\r?\n/);
      const symbols = ext === ".py" ? extractPythonDefs(text) : [];
      out.push({
        relPath: rel.split(path.sep).join("/"),
        symbols: symbols.slice(0, 20),
        lineCount: lines.length,
      });
    }
  }
}

/** Lightweight stand-in until web-tree-sitter + grammars are wired. */
function extractPythonDefs(source: string): string[] {
  const names: string[] = [];
  const re = /^\s*(?:async\s+)?def\s+([a-zA-Z_][\w]*)\s*\(/gm;
  let m: RegExpExecArray | null;
  while ((m = re.exec(source)) !== null) {
    names.push(m[1]);
  }
  const cre = /^\s*class\s+([a-zA-Z_][\w]*)\s*[:(]/gm;
  while ((m = cre.exec(source)) !== null) {
    names.push(m[1]);
  }
  return [...new Set(names)];
}

export function buildCompactRepoIndex(
  workspaceRoot: string,
  cfg: AutobotRepoIndexConfig
): RepoIndexEntry[] {
  const out: RepoIndexEntry[] = [];
  walk(workspaceRoot, workspaceRoot, 0, cfg.maxDepth, cfg.maxFiles, out);
  return out;
}

/**
 * Serializes index for Planner `REPO_SYMBOLS` slot (~truncate in orchestrator if too long).
 */
export function formatRepoSymbolsCompact(entries: RepoIndexEntry[], maxChars: number): string {
  const lines: string[] = [];
  for (const e of entries) {
    const sym = e.symbols.length ? ` [${e.symbols.slice(0, 8).join(", ")}]` : "";
    lines.push(`${e.relPath}:${e.lineCount}${sym}`);
  }
  let s = lines.join("\n");
  if (s.length > maxChars) {
    s = s.slice(0, maxChars) + "\n…(truncated)";
  }
  return s;
}

export async function getWorkspaceRoot(): Promise<string | undefined> {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders?.length) {
    return undefined;
  }
  return folders[0].uri.fsPath;
}

/**
 * Reads file chunks for paths mentioned in plan (very naive: match substrings).
 * Full Tree-sitter span extraction should replace this.
 */
export function extractCodeSpansForPlan(
  workspaceRoot: string,
  planText: string,
  entries: RepoIndexEntry[],
  maxCharsPerFile: number
): string {
  const blocks: string[] = [];
  for (const e of entries) {
    if (planText.includes(e.relPath) || planText.includes(e.relPath.replace(/\//g, "\\"))) {
      const full = path.join(workspaceRoot, e.relPath);
      let text = "";
      try {
        text = fs.readFileSync(full, "utf8");
      } catch {
        continue;
      }
      if (text.length > maxCharsPerFile) {
        text = text.slice(0, maxCharsPerFile) + "\n…(truncated)";
      }
      blocks.push(`=== ${e.relPath} ===\n${text}`);
    }
  }
  if (!blocks.length) {
    return "(no file paths from plan matched workspace index; paste target paths into plan)";
  }
  return blocks.join("\n\n");
}
