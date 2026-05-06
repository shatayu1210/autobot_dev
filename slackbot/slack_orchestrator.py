import asyncio
import threading
import time
from datetime import datetime
from typing import TypedDict, Annotated
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from langgraph.graph import StateGraph, END
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import (
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL,
    POLL_INTERVAL_SECONDS,
    SCORER_THRESHOLD
)
from poller import poll_once
from sentinel import run_sentinel
from reasoner import run_reasoner
from notifier import run_notifier
from adhoc import handle_adhoc_query

# ── Slack client for event handling ──────────────────────────────────────────
slack_client = WebClient(token=SLACK_BOT_TOKEN)


# ── LangGraph State ───────────────────────────────────────────────────────────

class PipelineState(TypedDict):
    """State that flows through the LangGraph pipeline."""
    # Input
    raw_issues:      list[dict]
    # After sentinel
    high_issues:     list[dict]
    # After reasoner
    analyzed_issues: list[dict]
    # After notifier
    sent_count:      int
    failed_count:    int
    # Metadata
    cycle_start:     str
    error:           str | None


# ── LangGraph Nodes ───────────────────────────────────────────────────────────

def node_poll(state: PipelineState) -> PipelineState:
    """Node 1 — Poll GitHub for new issues."""
    print(f"\n[LangGraph] node_poll running...")
    try:
        new_issues = poll_once()
        return {
            **state,
            "raw_issues":  new_issues,
            "cycle_start": datetime.now().isoformat(),
            "error":       None
        }
    except Exception as e:
        print(f"[LangGraph] node_poll error: {e}")
        return {**state, "raw_issues": [], "error": str(e)}


def node_score(state: PipelineState) -> PipelineState:
    """Node 2 — Sentinel scores each issue."""
    print(f"[LangGraph] node_score running on {len(state['raw_issues'])} issues...")
    if not state["raw_issues"]:
        return {**state, "high_issues": []}
    try:
        high_issues = run_sentinel(state["raw_issues"])
        return {**state, "high_issues": high_issues}
    except Exception as e:
        print(f"[LangGraph] node_score error: {e}")
        return {**state, "high_issues": [], "error": str(e)}


def node_threshold_check(state: PipelineState) -> str:
    """
    Conditional edge — decides next node based on whether
    any HIGH severity issues were found.
    Returns: 'reason' or 'skip'
    """
    if state.get("high_issues"):
        print(f"[LangGraph] threshold_check → {len(state['high_issues'])} HIGH issues → routing to Reasoner")
        return "reason"
    else:
        print(f"[LangGraph] threshold_check → no HIGH issues → skipping")
        return "skip"


def node_reason(state: PipelineState) -> PipelineState:
    """Node 3 — Reasoner analyzes HIGH severity issues."""
    print(f"[LangGraph] node_reason running on {len(state['high_issues'])} issues...")
    try:
        analyzed = run_reasoner(state["high_issues"])
        return {**state, "analyzed_issues": analyzed}
    except Exception as e:
        print(f"[LangGraph] node_reason error: {e}")
        return {**state, "analyzed_issues": [], "error": str(e)}


def node_notify(state: PipelineState) -> PipelineState:
    """Node 4 — Send Slack notifications."""
    print(f"[LangGraph] node_notify running on {len(state['analyzed_issues'])} issues...")
    try:
        result = run_notifier(state["analyzed_issues"])
        return {
            **state,
            "sent_count":   result["sent"],
            "failed_count": result["failed"]
        }
    except Exception as e:
        print(f"[LangGraph] node_notify error: {e}")
        return {**state, "sent_count": 0, "failed_count": 0, "error": str(e)}


def node_skip(state: PipelineState) -> PipelineState:
    """Node — No high severity issues, nothing to do."""
    print(f"[LangGraph] node_skip → cycle complete, no notifications sent")
    return {**state, "sent_count": 0, "failed_count": 0}



# ── Build LangGraph pipeline ──────────────────────────────────────────────────

def build_pipeline() -> any:
    """Build and compile the LangGraph state machine."""

    graph = StateGraph(PipelineState)

    # Add nodes
    graph.add_node("poll",   node_poll)
    graph.add_node("score",  node_score)
    graph.add_node("reason", node_reason)
    graph.add_node("notify", node_notify)
    graph.add_node("skip",   node_skip)

    # Entry point
    graph.set_entry_point("poll")

    # Edges
    graph.add_edge("poll", "score")

    # Conditional edge after scoring
    graph.add_conditional_edges(
        "score",
        node_threshold_check,
        {
            "reason": "reason",
            "skip":   "skip"
        }
    )

    graph.add_edge("reason", "notify")
    graph.add_edge("notify", END)
    graph.add_edge("skip",   END)

    return graph.compile()


pipeline = build_pipeline()
print("LangGraph pipeline compiled ✅")


# ── Polling loop (background thread) ─────────────────────────────────────────

def polling_loop():
    """Background thread — runs the LangGraph pipeline every POLL_INTERVAL_SECONDS."""
    print(f"Polling loop started — interval: {POLL_INTERVAL_SECONDS}s")

    while True:
        try:
            print(f"\n{'='*60}")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running pipeline cycle...")

            initial_state: PipelineState = {
                "raw_issues":      [],
                "high_issues":     [],
                "analyzed_issues": [],
                "sent_count":      0,
                "failed_count":    0,
                "cycle_start":     datetime.now().isoformat(),
                "error":           None
            }

            final_state = pipeline.invoke(initial_state)

            print(f"\n[Cycle Summary]")
            print(f"  Raw issues:    {len(final_state['raw_issues'])}")
            print(f"  HIGH severity: {len(final_state['high_issues'])}")
            print(f"  Analyzed:      {len(final_state['analyzed_issues'])}")
            print(f"  Slack sent:    {final_state['sent_count']}")
            print(f"  Failed:        {final_state['failed_count']}")
            if final_state.get("error"):
                print(f"  Error:         {final_state['error']}")

        except Exception as e:
            print(f"[Polling loop error]: {e}")

        print(f"\nSleeping {POLL_INTERVAL_SECONDS}s until next cycle...")
        time.sleep(POLL_INTERVAL_SECONDS)


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background polling thread when FastAPI starts."""
    print("Starting AutoBot Orchestrator...")
    thread = threading.Thread(target=polling_loop, daemon=True)
    thread.start()
    print("Background polling thread started ✅")
    yield
    print("Shutting down AutoBot Orchestrator...")


app = FastAPI(
    title="AutoBot Slack Orchestrator",
    description="Polling + Adhoc query orchestrator for AutoBot Slack feature",
    lifespan=lifespan
)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "service": "autobot-slack-orchestrator",
        "timestamp": datetime.now().isoformat()
    }


@app.post("/poll")
async def trigger_poll(background_tasks: BackgroundTasks):
    """
    Manually trigger one poll cycle.
    Useful for testing without waiting for the interval.
    """
    def run_cycle():
        initial_state: PipelineState = {
            "raw_issues":      [],
            "high_issues":     [],
            "analyzed_issues": [],
            "sent_count":      0,
            "failed_count":    0,
            "cycle_start":     datetime.now().isoformat(),
            "error":           None
        }
        final_state = pipeline.invoke(initial_state)
        print(f"[Manual poll] sent={final_state['sent_count']} failed={final_state['failed_count']}")

    background_tasks.add_task(run_cycle)
    return {"status": "poll cycle triggered"}


def process_adhoc_query(clean_query: str, issue_number: int | None, channel: str, thread_ts: str):
    """Background task to process adhoc query and send reply."""
    try:
        response_text = handle_adhoc_query(clean_query, issue_number)
        slack_client.chat_postMessage(
            channel=channel,
            text=response_text,
            thread_ts=thread_ts
        )
    except SlackApiError as e:
        print(f"[Adhoc] Slack reply failed: {e}")
    except Exception as e:
        print(f"[Adhoc] Processing failed: {e}")


@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    """
    Slack event handler.
    Handles:
      - URL verification challenge (one-time Slack setup)
      - App mention events → adhoc query path
    """
    body = await request.json()

    # Slack URL verification
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body["challenge"]})

    # Slack Event Retries: ignore them so we don't process the same query 3 times
    if request.headers.get("X-Slack-Retry-Num"):
        return JSONResponse({"status": "ignored retry"})

    # Handle events
    event = body.get("event", {})
    event_type = event.get("type")

    if event_type == "app_mention":
        user_query = event.get("text", "")
        channel    = event.get("channel", SLACK_CHANNEL)
        thread_ts  = event.get("ts")

        # Strip bot mention from query (@AutoBot what is...)
        import re
        clean_query = re.sub(r"<@[A-Z0-9]+>", "", user_query).strip()

        # Extract issue number if mentioned (#12345)
        issue_match  = re.search(r"#(\d+)", clean_query)
        issue_number = int(issue_match.group(1)) if issue_match else None

        print(f"[Adhoc] query='{clean_query}' issue={issue_number}")

        # Run the heavy LLM processing in the background so we can instantly return 200 to Slack
        background_tasks.add_task(process_adhoc_query, clean_query, issue_number, channel, thread_ts)

    return JSONResponse({"status": "ok"})


@app.get("/status")
async def status():
    """Returns current pipeline configuration."""
    return {
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "scorer_threshold":      SCORER_THRESHOLD,
        "slack_channel":         SLACK_CHANNEL,
        "pipeline_nodes": [
            "poll → score → threshold_check → reason → notify"
        ],
        "adhoc_status": "live — GitHub APIs + GraphRAG + LLM tool planner active"
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "slack_orchestrator:app",
        host="0.0.0.0",
        port=8000,
        reload=False
    )
