import type { CriticResult, CriticVerdict, PlannerResult } from "./types";

export function parsePlannerOutput(raw: string): PlannerResult {
  const upper = raw.toUpperCase();
  let requiresCodeChange: boolean | null = null;
  if (/REQUIRES_CODE_CHANGE:\s*YES/.test(upper)) {
    requiresCodeChange = true;
  } else if (/REQUIRES_CODE_CHANGE:\s*NO/.test(upper)) {
    requiresCodeChange = false;
  }
  let confidence: PlannerResult["confidence"] = null;
  const confMatch = upper.match(/CONFIDENCE:\s*(HIGH|MEDIUM|LOW)/);
  if (confMatch) {
    confidence = confMatch[1] as PlannerResult["confidence"];
  }
  let reason: string | null = null;
  const reasonMatch = raw.match(/REASON:\s*([^\n]+)/i);
  if (reasonMatch) {
    reason = reasonMatch[1].trim();
  }
  let planBody: string | null = null;
  const planIdx = upper.indexOf("PLAN:");
  if (planIdx >= 0) {
    planBody = raw.slice(planIdx).replace(/^PLAN:\s*/i, "").trim();
  }
  return { raw, requiresCodeChange, confidence, reason, planBody };
}

export function parseCriticOutput(raw: string): CriticResult {
  const trimmed = raw.trim();
  const firstLine = trimmed.split(/\r?\n/)[0] || "";
  let verdict: CriticVerdict | null = null;
  if (/^\s*ACCEPT\b/i.test(firstLine)) {
    verdict = "ACCEPT";
  } else if (/^\s*REVISE\b/i.test(firstLine)) {
    verdict = "REVISE";
  } else if (/^\s*REJECT\b/i.test(firstLine)) {
    verdict = "REJECT";
  }
  const reasoning = trimmed.slice(firstLine.length).trim();
  return { raw, verdict, reasoning };
}
