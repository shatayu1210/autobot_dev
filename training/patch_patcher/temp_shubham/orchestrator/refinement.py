from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha1
from typing import Dict, Iterable, List, Optional, Protocol, Sequence, Set, Tuple


@dataclass(frozen=True)
class ContextBudget:
    """Hard caps for deterministic refinement packing."""

    model_context_tokens: int = 16384
    reserved_prompt_and_output_tokens: int = 4096
    max_snippets: int = 8
    max_chars_per_snippet: int = 1000
    max_total_evidence_chars: int = 10000
    max_total_evidence_tokens: Optional[int] = None

    @property
    def available_tokens(self) -> int:
        return max(self.model_context_tokens - self.reserved_prompt_and_output_tokens, 0)


@dataclass(frozen=True)
class OrchestratorConfig:
    """Deterministic trigger thresholds and loop limits."""

    weak_no_confidence_threshold: float = 0.70
    min_useful_spans: int = 2
    min_path_overlap_ratio: float = 0.25
    low_evidence_quality_threshold: float = 0.40
    max_escalation_level: int = 3
    max_patcher_attempts: int = 3
    min_patcher_confidence_for_critic: float = 0.55
    context_budget: ContextBudget = field(default_factory=ContextBudget)


@dataclass(frozen=True)
class PlannerDecision:
    requires_code_change: str
    confidence: float
    reason: str
    target_files: List[str]
    useful_spans_count: int = 0
    evidence_quality_score: float = 1.0


@dataclass(frozen=True)
class EvidenceSnippet:
    path: str
    symbol: str
    excerpt: str
    reason_tag: str
    score: float
    source: str
    is_delta: bool = True

    def clipped(self, max_chars: int) -> "EvidenceSnippet":
        clipped_excerpt = self.excerpt[:max_chars]
        return EvidenceSnippet(
            path=self.path,
            symbol=self.symbol,
            excerpt=clipped_excerpt,
            reason_tag=self.reason_tag,
            score=self.score,
            source=self.source,
            is_delta=self.is_delta,
        )


@dataclass(frozen=True)
class TriggerFlags:
    weak_no: bool
    sparse_spans: bool
    path_mismatch: bool
    low_evidence_quality: bool
    missing_target_path: bool

    @property
    def should_refine(self) -> bool:
        return any(
            [
                self.weak_no,
                self.sparse_spans,
                self.path_mismatch,
                self.low_evidence_quality,
                self.missing_target_path,
            ]
        )

    def active_flags(self) -> List[str]:
        flags = []
        if self.weak_no:
            flags.append("weak_no")
        if self.sparse_spans:
            flags.append("sparse_spans")
        if self.path_mismatch:
            flags.append("path_mismatch")
        if self.low_evidence_quality:
            flags.append("low_evidence_quality")
        if self.missing_target_path:
            flags.append("missing_target_path")
        return flags


@dataclass(frozen=True)
class RefinementDecision:
    should_refine: bool
    escalation_start_level: int
    trigger_flags: TriggerFlags


@dataclass(frozen=True)
class RefinementOutcome:
    """Payload to feed back into planner/patcher second pass."""

    initial_summary: Dict[str, object]
    new_evidence: List[EvidenceSnippet]
    reranked_candidates: List[Dict[str, object]]
    conflicts: List[Dict[str, object]]
    revision_instruction: str
    dropped_snippets_count: int
    dropped_chars_count: int
    escalation_reached: int


@dataclass(frozen=True)
class PatcherAttempt:
    attempt_id: int
    apply_ok: bool
    allowed_files_ok: bool
    valid_unified_diff: bool
    patch_confidence: float


@dataclass(frozen=True)
class PatcherLoopState:
    attempts: List[PatcherAttempt]
    max_attempts: int

    @property
    def last_attempt(self) -> Optional[PatcherAttempt]:
        return self.attempts[-1] if self.attempts else None


class OrchestratorToolset(Protocol):
    """Protocol that maps to VSCode orchestrator repo tools."""

    def path_exists(self, repo_path: str) -> bool:
        ...

    def query_graphrag(self, query: str, top_k: int, wide: bool = False) -> List[EvidenceSnippet]:
        ...

    def keyword_search(self, query_terms: Sequence[str], top_k: int) -> List[EvidenceSnippet]:
        ...

    def read_line_level(self, file_path: str, hint_terms: Sequence[str], top_k: int) -> List[EvidenceSnippet]:
        ...

    def trace_interfaces(self, file_path: str, symbol_hints: Sequence[str], top_k: int) -> List[EvidenceSnippet]:
        ...


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _normalize_target_files(paths: Iterable[str]) -> Set[str]:
    return {p.strip() for p in paths if p and p.strip()}


def evaluate_triggers(
    planner: PlannerDecision,
    graphrag_top_paths: Sequence[str],
    missing_target_paths: Sequence[str],
    config: OrchestratorConfig,
) -> TriggerFlags:
    planner_paths = _normalize_target_files(planner.target_files)
    rag_paths = _normalize_target_files(graphrag_top_paths)
    overlap = len(planner_paths.intersection(rag_paths))
    overlap_ratio = _safe_ratio(overlap, len(planner_paths))

    weak_no = planner.requires_code_change.upper() == "NO" and planner.confidence < config.weak_no_confidence_threshold
    sparse_spans = planner.useful_spans_count < config.min_useful_spans
    path_mismatch = bool(planner_paths) and overlap_ratio < config.min_path_overlap_ratio
    low_evidence_quality = planner.evidence_quality_score < config.low_evidence_quality_threshold
    missing_target_path = len(list(missing_target_paths)) > 0

    return TriggerFlags(
        weak_no=weak_no,
        sparse_spans=sparse_spans,
        path_mismatch=path_mismatch,
        low_evidence_quality=low_evidence_quality,
        missing_target_path=missing_target_path,
    )


def decide_refinement(trigger_flags: TriggerFlags) -> RefinementDecision:
    """
    Deterministic policy:
    - any flag => refinement
    - missing target path jumps to level 1 directly
    """
    start_level = 1 if trigger_flags.missing_target_path else 0
    return RefinementDecision(
        should_refine=trigger_flags.should_refine,
        escalation_start_level=start_level,
        trigger_flags=trigger_flags,
    )


def _dedupe_key(snippet: EvidenceSnippet) -> str:
    normalized = "|".join(
        [
            snippet.path.strip().lower(),
            snippet.symbol.strip().lower(),
            snippet.reason_tag.strip().lower(),
            snippet.excerpt.strip().lower(),
        ]
    )
    return sha1(normalized.encode("utf-8")).hexdigest()


def _select_diverse_snippets(
    snippets: Sequence[EvidenceSnippet],
    budget: ContextBudget,
    already_seen_keys: Optional[Set[str]] = None,
) -> Tuple[List[EvidenceSnippet], int, int]:
    """
    Returns selected snippets, dropped_snippet_count, dropped_char_count.
    Deterministic selection:
    1) sort by score desc,
    2) dedupe near-identical snippets,
    3) enforce file diversity before allowing repeats,
    4) stop when snippet/char caps are hit.
    """
    seen_keys = set(already_seen_keys or set())
    ranked = sorted(snippets, key=lambda s: s.score, reverse=True)
    clipped_ranked = [snip.clipped(budget.max_chars_per_snippet) for snip in ranked if snip.is_delta]

    selected: List[EvidenceSnippet] = []
    selected_paths: Set[str] = set()
    dropped_count = 0
    dropped_chars = 0
    used_chars = 0
    used_tokens = 0

    def estimate_tokens(text: str) -> int:
        # deterministic lightweight estimate suitable for orchestration gating
        return max(1, len(text) // 4)

    # Pass A: diversity-first (one per file)
    for snippet in clipped_ranked:
        if len(selected) >= budget.max_snippets:
            dropped_count += 1
            dropped_chars += len(snippet.excerpt)
            continue

        key = _dedupe_key(snippet)
        if key in seen_keys:
            dropped_count += 1
            dropped_chars += len(snippet.excerpt)
            continue

        if snippet.path in selected_paths:
            continue

        if used_chars + len(snippet.excerpt) > budget.max_total_evidence_chars:
            dropped_count += 1
            dropped_chars += len(snippet.excerpt)
            continue
        snippet_tokens = estimate_tokens(snippet.excerpt)
        if (
            budget.max_total_evidence_tokens is not None
            and used_tokens + snippet_tokens > budget.max_total_evidence_tokens
        ):
            dropped_count += 1
            dropped_chars += len(snippet.excerpt)
            continue

        selected.append(snippet)
        selected_paths.add(snippet.path)
        seen_keys.add(key)
        used_chars += len(snippet.excerpt)
        used_tokens += snippet_tokens

    # Pass B: fill remaining slots regardless of file, still deduped and capped
    if len(selected) < budget.max_snippets and used_chars < budget.max_total_evidence_chars:
        for snippet in clipped_ranked:
            if len(selected) >= budget.max_snippets:
                break

            key = _dedupe_key(snippet)
            if key in seen_keys:
                continue

            if used_chars + len(snippet.excerpt) > budget.max_total_evidence_chars:
                dropped_count += 1
                dropped_chars += len(snippet.excerpt)
                continue
            snippet_tokens = estimate_tokens(snippet.excerpt)
            if (
                budget.max_total_evidence_tokens is not None
                and used_tokens + snippet_tokens > budget.max_total_evidence_tokens
            ):
                dropped_count += 1
                dropped_chars += len(snippet.excerpt)
                continue

            selected.append(snippet)
            seen_keys.add(key)
            used_chars += len(snippet.excerpt)
            used_tokens += snippet_tokens

    # Account for ranked snippets not selected
    selected_keys = {_dedupe_key(s) for s in selected}
    for snippet in clipped_ranked:
        if _dedupe_key(snippet) not in selected_keys:
            dropped_count += 1
            dropped_chars += len(snippet.excerpt)

    return selected, dropped_count, dropped_chars


def _collect_level_evidence(
    level: int,
    tools: OrchestratorToolset,
    query: str,
    query_terms: Sequence[str],
    candidate_files: Sequence[str],
) -> List[EvidenceSnippet]:
    if level == 0:
        return tools.query_graphrag(query=query, top_k=8, wide=False)
    if level == 1:
        return tools.query_graphrag(query=query, top_k=16, wide=True) + tools.keyword_search(query_terms, top_k=10)
    if level == 2:
        out: List[EvidenceSnippet] = []
        for file_path in candidate_files[:6]:
            out.extend(tools.read_line_level(file_path=file_path, hint_terms=query_terms, top_k=4))
            out.extend(tools.trace_interfaces(file_path=file_path, symbol_hints=query_terms, top_k=4))
        return out
    # Level 3 deep fallback (bounded)
    out = []
    for file_path in candidate_files[:10]:
        out.extend(tools.read_line_level(file_path=file_path, hint_terms=query_terms, top_k=6))
        out.extend(tools.trace_interfaces(file_path=file_path, symbol_hints=query_terms, top_k=6))
    return out


def _evidence_sufficient(snippets: Sequence[EvidenceSnippet]) -> bool:
    if len(snippets) >= 6:
        return True
    high_signal = [s for s in snippets if s.score >= 0.75]
    return len(high_signal) >= 3


def run_orchestrator_refinement(
    planner_pass1: PlannerDecision,
    graphrag_top_paths: Sequence[str],
    tools: OrchestratorToolset,
    issue_query: str,
    query_terms: Sequence[str],
    pass1_evidence_fingerprints: Optional[Set[str]] = None,
    config: Optional[OrchestratorConfig] = None,
) -> Optional[RefinementOutcome]:
    """
    Deterministic single-pass refinement.
    Returns None when refinement should not run.
    """
    cfg = config or OrchestratorConfig()
    missing_paths = [p for p in planner_pass1.target_files if p and not tools.path_exists(p)]
    trigger_flags = evaluate_triggers(
        planner=planner_pass1,
        graphrag_top_paths=graphrag_top_paths,
        missing_target_paths=missing_paths,
        config=cfg,
    )
    decision = decide_refinement(trigger_flags)
    if not decision.should_refine:
        return None

    collected: List[EvidenceSnippet] = []
    escalation_reached = decision.escalation_start_level
    candidate_files = list(dict.fromkeys(list(planner_pass1.target_files) + list(graphrag_top_paths)))

    for level in range(decision.escalation_start_level, cfg.max_escalation_level + 1):
        level_snippets = _collect_level_evidence(
            level=level,
            tools=tools,
            query=issue_query,
            query_terms=query_terms,
            candidate_files=candidate_files,
        )
        collected.extend(level_snippets)
        escalation_reached = level
        if _evidence_sufficient(collected):
            break

    seen_keys = set(pass1_evidence_fingerprints or set())
    selected, dropped_count, dropped_chars = _select_diverse_snippets(
        snippets=collected,
        budget=cfg.context_budget,
        already_seen_keys=seen_keys,
    )

    reranked_candidates = []
    conflicts = []
    by_path: Dict[str, List[EvidenceSnippet]] = {}
    for snip in selected:
        by_path.setdefault(snip.path, []).append(snip)

    for path, snippets in by_path.items():
        avg_score = sum(s.score for s in snippets) / len(snippets)
        tags = sorted({s.reason_tag for s in snippets})
        reranked_candidates.append({"path": path, "score": round(avg_score, 4), "tags": tags})
        has_conflict = "conflict_path" in tags and "supports_path" in tags
        if has_conflict:
            conflicts.append(
                {
                    "path": path,
                    "type": "path_conflict",
                    "support_count": len([s for s in snippets if s.reason_tag == "supports_path"]),
                    "conflict_count": len([s for s in snippets if s.reason_tag == "conflict_path"]),
                }
            )

    reranked_candidates = sorted(reranked_candidates, key=lambda r: float(r["score"]), reverse=True)
    active_flags = trigger_flags.active_flags()
    revision_instruction = (
        "Revise the previous decision using only NEW_EVIDENCE delta. "
        "Keep constraints unchanged. Prefer edits in existing files. "
        "If any target file is missing, pick nearest existing candidate path from reranked list. "
        f"Trigger reasons: {', '.join(active_flags)}."
    )

    return RefinementOutcome(
        initial_summary={
            "requires_code_change": planner_pass1.requires_code_change,
            "confidence": planner_pass1.confidence,
            "target_files": planner_pass1.target_files,
            "reason": planner_pass1.reason,
            "trigger_flags": active_flags,
            "missing_target_paths": missing_paths,
        },
        new_evidence=selected,
        reranked_candidates=reranked_candidates,
        conflicts=conflicts,
        revision_instruction=revision_instruction,
        dropped_snippets_count=dropped_count,
        dropped_chars_count=dropped_chars,
        escalation_reached=escalation_reached,
    )


def should_handoff_to_critic(
    loop_state: PatcherLoopState,
    config: Optional[OrchestratorConfig] = None,
) -> bool:
    """
    Stop patcher loop and hand off to critic when:
    - latest attempt is structurally valid + allowed-file compliant + apply_ok,
    - and confidence meets floor,
    OR
    - max attempts reached (critic gets best failed attempt with diagnostics).
    """
    cfg = config or OrchestratorConfig()
    last = loop_state.last_attempt
    if last is None:
        return False

    success_gate = (
        last.apply_ok
        and last.allowed_files_ok
        and last.valid_unified_diff
        and last.patch_confidence >= cfg.min_patcher_confidence_for_critic
    )
    if success_gate:
        return True

    return len(loop_state.attempts) >= min(loop_state.max_attempts, cfg.max_patcher_attempts)


class DeterministicOrchestrator:
    """
    Single entrypoint wrapper so callers can use one orchestrator object.

    It centralizes:
    - refinement trigger + escalation + evidence packing
    - critic handoff stop policy
    """

    def __init__(self, config: Optional[OrchestratorConfig] = None):
        self.config = config or OrchestratorConfig()

    def refine(
        self,
        planner_pass1: PlannerDecision,
        graphrag_top_paths: Sequence[str],
        tools: OrchestratorToolset,
        issue_query: str,
        query_terms: Sequence[str],
        pass1_evidence_fingerprints: Optional[Set[str]] = None,
    ) -> Optional[RefinementOutcome]:
        return run_orchestrator_refinement(
            planner_pass1=planner_pass1,
            graphrag_top_paths=graphrag_top_paths,
            tools=tools,
            issue_query=issue_query,
            query_terms=query_terms,
            pass1_evidence_fingerprints=pass1_evidence_fingerprints,
            config=self.config,
        )

    def should_handoff(self, loop_state: PatcherLoopState) -> bool:
        return should_handoff_to_critic(loop_state=loop_state, config=self.config)
