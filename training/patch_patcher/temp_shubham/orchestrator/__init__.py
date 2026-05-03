"""Deterministic orchestrator package for planner/patcher refinement."""

from .refinement import (
    ContextBudget,
    DeterministicOrchestrator,
    EvidenceSnippet,
    OrchestratorConfig,
    PlannerDecision,
    PatcherAttempt,
    PatcherLoopState,
    RefinementDecision,
    RefinementOutcome,
    TriggerFlags,
    decide_refinement,
    evaluate_triggers,
    run_orchestrator_refinement,
    should_handoff_to_critic,
)
from .model_router import EndpointConfig, HFModelRouter, RouterConfig
from .telemetry import JsonlTelemetryLogger

__all__ = [
    "ContextBudget",
    "DeterministicOrchestrator",
    "EvidenceSnippet",
    "OrchestratorConfig",
    "PlannerDecision",
    "PatcherAttempt",
    "PatcherLoopState",
    "RefinementDecision",
    "RefinementOutcome",
    "TriggerFlags",
    "decide_refinement",
    "evaluate_triggers",
    "run_orchestrator_refinement",
    "should_handoff_to_critic",
    "EndpointConfig",
    "HFModelRouter",
    "RouterConfig",
    "JsonlTelemetryLogger",
]
