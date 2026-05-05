from typing import TypedDict, Annotated
import operator


class PipelineState(TypedDict):
    # Input
    issue_ref: str           # raw input e.g. "apache/airflow issue #64060"
    owner: str
    repo: str
    issue_number: int
    # Data
    issue_data: dict         # from GitHub API
    patch_plan: str          # planner output
    patch_diff: str          # patcher output
    # Critic
    verdict: str             # ACCEPT / REVISE / REJECT / ""
    feedback: str
    # Control
    iteration: int           # current loop iteration (starts 0)
    max_iterations: int      # default 3
    # Output
    final_result: str        # formatted markdown result
    progress: Annotated[list[str], operator.add]  # progress messages for streaming
