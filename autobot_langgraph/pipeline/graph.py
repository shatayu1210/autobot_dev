from langgraph.graph import StateGraph, START, END

from .state import PipelineState
from .nodes import parse_input_node, planner_node, patcher_node, critic_node, output_node


def route_after_critic(state: PipelineState) -> str:
    """Conditional routing after the Critic node."""
    if state["verdict"] == "ACCEPT":
        return "output"
    if state["verdict"] == "REJECT":
        return "output"
    if state["iteration"] >= state["max_iterations"]:
        return "output"
    return "patcher"  # REVISE — loop back


def build_graph():
    """Build and compile the LangGraph StateGraph for the patch pipeline."""
    g = StateGraph(PipelineState)

    g.add_node("parse_input", parse_input_node)
    g.add_node("planner", planner_node)
    g.add_node("patcher", patcher_node)
    g.add_node("critic", critic_node)
    g.add_node("output", output_node)

    g.add_edge(START, "parse_input")
    g.add_edge("parse_input", "planner")
    g.add_edge("planner", "patcher")
    g.add_edge("patcher", "critic")
    g.add_conditional_edges(
        "critic",
        route_after_critic,
        {"output": "output", "patcher": "patcher"},
    )
    g.add_edge("output", END)

    return g.compile()


graph = build_graph()
