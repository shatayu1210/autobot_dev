import os
import json
import time
from typing import AsyncGenerator
from google.adk.agents import BaseAgent, LoopAgent, SequentialAgent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.events import Event, EventActions
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.callback_context import CallbackContext

from traced_authenticated_httpx import create_traced_authenticated_client  # type: ignore


# --- Callbacks ---

_agent_start_times: dict = {}


def create_save_output_callback(key: str):
    """Creates a callback to save the agent's final response to session state."""
    def callback(callback_context: CallbackContext, **kwargs) -> None:
        ctx = callback_context
        agent = ctx.agent_name
        elapsed = time.time() - _agent_start_times.pop(agent, time.time())
        for event in reversed(ctx.session.events):
            if event.author == agent and event.content and event.content.parts:
                text = event.content.parts[0].text
                if text:
                    if key == "critic_verdict" and text.strip().startswith("{"):
                        try:
                            ctx.state[key] = json.loads(text)
                        except json.JSONDecodeError:
                            ctx.state[key] = text
                    else:
                        ctx.state[key] = text
                    print(f"[TIMER] {agent} finished in {elapsed:.1f}s → saved to state['{key}']")
                    return
        print(f"[TIMER] {agent} finished in {elapsed:.1f}s → no text output captured")
    return callback


def create_before_callback(agent_name: str):
    """Records start time before each agent runs."""
    def callback(callback_context: CallbackContext, **kwargs) -> None:
        _agent_start_times[agent_name] = time.time()
        print(f"[TIMER] {agent_name} starting...")
    return callback


# --- Remote Agents ---

# Planner — runs on port 8001
planner_url = os.environ.get(
    "PLANNER_AGENT_CARD_URL",
    "http://localhost:8001/a2a/agent/.well-known/agent-card.json",
)
planner = RemoteA2aAgent(
    name="planner",
    agent_card=planner_url,
    description="Analyzes GitHub issues and generates structured patch plans.",
    before_agent_callback=create_before_callback("planner"),
    after_agent_callback=create_save_output_callback("patch_plan"),
    httpx_client=create_traced_authenticated_client(planner_url),
)

# Patcher — runs on port 8003
patcher_url = os.environ.get(
    "PATCHER_AGENT_CARD_URL",
    "http://localhost:8003/a2a/agent/.well-known/agent-card.json",
)
patcher = RemoteA2aAgent(
    name="patcher",
    agent_card=patcher_url,
    description="Generates unified diffs from structured plans.",
    before_agent_callback=create_before_callback("patcher"),
    after_agent_callback=create_save_output_callback("patch_diff"),
    httpx_client=create_traced_authenticated_client(patcher_url),
)

# Critic — runs on port 8002
critic_url = os.environ.get(
    "CRITIC_AGENT_CARD_URL",
    "http://localhost:8002/a2a/agent/.well-known/agent-card.json",
)
critic = RemoteA2aAgent(
    name="critic",
    agent_card=critic_url,
    description="Evaluates diffs and returns ACCEPT/REVISE/REJECT verdict.",
    before_agent_callback=create_before_callback("critic"),
    after_agent_callback=create_save_output_callback("critic_verdict"),
    httpx_client=create_traced_authenticated_client(critic_url),
)


# --- Escalation / Loop Control ---

class VerdictChecker(BaseAgent):
    """Checks the critic's verdict and escalates (breaks the loop) on ACCEPT or REJECT.

    The Patcher-Critic loop should continue only on REVISE. On ACCEPT, we
    break and present the patch. On REJECT, we break and report failure.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        verdict_data = ctx.session.state.get("critic_verdict")
        print(f"[VerdictChecker] Verdict received: {verdict_data}")

        verdict_str = ""
        if isinstance(verdict_data, dict):
            verdict_str = verdict_data.get("verdict", "").upper()
        elif isinstance(verdict_data, str):
            try:
                parsed = json.loads(verdict_data)
                verdict_str = parsed.get("verdict", "").upper()
            except (json.JSONDecodeError, AttributeError):
                upper_text = verdict_data.upper()
                if "ACCEPT" in upper_text:
                    verdict_str = "ACCEPT"
                elif "REJECT" in upper_text:
                    verdict_str = "REJECT"
                else:
                    verdict_str = "REVISE"

        if verdict_str == "ACCEPT":
            ctx.session.state["loop_exit_reason"] = "ACCEPT"
            yield Event(author=self.name, actions=EventActions(escalate=True))
        elif verdict_str == "REJECT":
            ctx.session.state["loop_exit_reason"] = "REJECT"
            yield Event(author=self.name, actions=EventActions(escalate=True))
        else:
            ctx.session.state["loop_exit_reason"] = "REVISE"
            yield Event(author=self.name)


verdict_checker = VerdictChecker(name="verdict_checker")


# --- Patch Output Agent ---

class PatchOutputAgent(BaseAgent):
    """Presents the final pipeline result to the user.

    On ACCEPT: outputs the accepted unified diff and plan summary so the
    user can review and apply it manually (e.g., git apply, open a PR).
    On REJECT: reports that the diff was rejected with critic feedback.
    On exhausted iterations: shows the last diff and feedback for the user
    to decide next steps.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        exit_reason = ctx.session.state.get("loop_exit_reason", "")
        diff = ctx.session.state.get("patch_diff", "")
        plan = ctx.session.state.get("patch_plan", "")
        critic_verdict = ctx.session.state.get("critic_verdict", "")

        # Extract feedback string from critic verdict
        feedback = ""
        if isinstance(critic_verdict, dict):
            feedback = critic_verdict.get("feedback", "")
        elif isinstance(critic_verdict, str):
            try:
                parsed = json.loads(critic_verdict)
                feedback = parsed.get("feedback", "")
            except (json.JSONDecodeError, AttributeError):
                feedback = critic_verdict

        if exit_reason == "ACCEPT" and diff:
            yield Event(
                author=self.name,
                content=_make_content(
                    "## Patch Accepted\n\n"
                    "The Critic approved the following diff. You can apply it with "
                    "`git apply` or use it to open a pull request.\n\n"
                    f"### Plan Summary\n{plan}\n\n"
                    f"### Unified Diff\n```diff\n{diff}\n```\n\n"
                    f"### Critic Feedback\n{feedback}"
                ),
            )
        elif exit_reason == "REJECT":
            yield Event(
                author=self.name,
                content=_make_content(
                    "## Patch Rejected\n\n"
                    "The Critic rejected the diff as fundamentally flawed.\n\n"
                    f"### Critic Feedback\n{feedback}\n\n"
                    "Consider revising the issue description or re-running the "
                    "pipeline with more context."
                ),
            )
        else:
            # Exhausted REVISE iterations or no diff produced
            yield Event(
                author=self.name,
                content=_make_content(
                    "## Patch Loop Exhausted\n\n"
                    "The Patcher-Critic loop reached maximum iterations without "
                    "an ACCEPT verdict.\n\n"
                    f"### Last Critic Feedback\n{feedback}\n\n"
                    + (
                        f"### Last Diff\n```diff\n{diff}\n```\n\n"
                        if diff else "No diff was produced.\n\n"
                    )
                    + "You may apply the last diff manually or re-run the pipeline."
                ),
            )


patch_output_agent = PatchOutputAgent(name="patch_output")


# --- Helper Functions ---

def _make_content(text: str):
    """Creates a Content object with a single text Part."""
    from google.genai.types import Content, Part
    return Content(parts=[Part(text=text)])


# --- Orchestration Pipeline ---

# Inner loop: Patcher → Critic → VerdictChecker
# Continues on REVISE, breaks on ACCEPT or REJECT (max 3 iterations)
patch_critic_loop = LoopAgent(
    name="patch_critic_loop",
    description=(
        "Iteratively generates and critiques diffs until the critic accepts "
        "or rejects, or max iterations (3) are reached."
    ),
    sub_agents=[patcher, critic, verdict_checker],
    max_iterations=3,
)

# Full pipeline: Planner → [Patcher ↔ Critic loop] → Patch Output
root_agent = SequentialAgent(
    name="vscode_code_pipeline",
    description=(
        "End-to-end VS Code code pipeline: analyzes a GitHub issue, "
        "generates a plan, iteratively patches and critiques, then "
        "presents the final diff for the user to apply."
    ),
    sub_agents=[planner, patch_critic_loop, patch_output_agent],
)
