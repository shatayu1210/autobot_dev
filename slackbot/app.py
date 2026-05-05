import json
import os
import re

VERSION = "v21.0.0"
print(f"BOOT: Starting slackbot version {VERSION}")
print(f"BOOT: Environment SNOWFLAKE_ACCOUNT={os.getenv('SNOWFLAKE_ACCOUNT')}")

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from google.cloud import aiplatform

from langchain_google_vertexai import ChatVertexAI
from langchain_core.tools import tool
from langchain_classic.agents import create_tool_calling_agent, AgentExecutor
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage

CHAT_HISTORY = {}

# Bottleneck prompt-engineering helpers (bottleneck_issue_to_msg.py)
import bottleneck_issue_to_msg as btm

import dpo_feedback

load_dotenv()

# ── Vertex AI / GCP setup ─────────────────────────────────────────────────────
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "YOUR_GCP_PROJECT_ID")
GCP_LOCATION   = os.environ.get("GCP_LOCATION", "us-central1")

try:
    if GCP_PROJECT_ID != "YOUR_GCP_PROJECT_ID":
        import vertexai
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_LOCATION)
    else:
        print("Set GCP_PROJECT_ID to initialize Vertex AI.")
except Exception as e:
    print(f"Failed to initialize Vertex AI: {e}")

# ── Regex to pull issue numbers from Slack messages ───────────────────────────
# Matches: #13696 | issue 13696 | issue #13696 | issue-number: 13696
_ISSUE_RE = re.compile(
    r"#(\d+)|issue[_ -]?(?:number[: #]*)?(\d+)",
    re.IGNORECASE,
)


def _format_bottleneck_response(raw: str) -> str:
    """Parse model JSON and render a Slack-friendly message."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return f"🔍 *Bottleneck Analysis*\n{raw}"

    score      = data.get("teacher_risk_score", "N/A")
    confidence = data.get("teacher_confidence", "N/A")
    reasons    = data.get("teacher_reasons", [])

    try:
        filled = round(float(score) * 10)
        bar = "🟥" * filled + "⬜" * (10 - filled)
    except (TypeError, ValueError):
        bar = ""

    lines = [
        "🔍 *Bottleneck Risk Analysis*",
        f"*Risk Score:* `{score}` / 1.0  {bar}",
        f"*Confidence:* `{confidence}`",
        "",
        "*Signal Observations:*",
    ]
    for r in reasons:
        lines.append(f"• *{r.get('signal', '')}*: {r.get('observation', '')}")
    if not reasons:
        lines.append("_No specific signals noted._")

    return "\n".join(lines)


# ── Bottleneck tool ───────────────────────────────────────────────────────────

@tool
def bottleneck_predictor(input_text: str) -> str:
    """Use when the user asks about bottlenecks, stalled issues, or blocked
    GitHub issues. Provide an issue number (e.g. #13696) or a title keyword."""

    # Extract issue number if present
    match = _ISSUE_RE.search(input_text)
    issue_number = int(match.group(1) or match.group(2)) if match else None

    try:
        if issue_number is not None:
            print(f"DEBUG: Fetching from Snowflake for issue #{issue_number}...")
            # Use module level constants to ensure they pick up latest environment
            row = btm._fetch_issue_row_by_where(
                "ISSUE_NUMBER = %s", (issue_number,),
                schema=btm.SF_SCHEMA, table=btm.SF_TABLE,
            )
        else:
            keyword = re.sub(r"<@[^>]+>", "", input_text).strip()
            print(f"DEBUG: Fetching from Snowflake for title keyword: {keyword}...")
            row = btm._fetch_issue_row_by_where(
                "TITLE ILIKE %s", (f"%{keyword}%",),
                schema=btm.SF_SCHEMA, table=btm.SF_TABLE,
            )
    except KeyError as e:
        print(f"DEBUG: Issue not found in Snowflake: {e}")
        return f"⚠️ Issue not found in Snowflake: {e}"
    except RuntimeError as e:
        print(f"DEBUG: Snowflake error: {e}")
        return f"⚠️ Snowflake error: {e}"

    # Build the structured system + user messages payload
    # Build the user prompt
    print(f"DEBUG: Using btm module for build_user_prompt. Version check: {VERSION}")
    user_prompt = btm.build_user_prompt(row)

    try:
        endpoint_id = os.environ["BOTTLENECK_ENDPOINT_ID"]
        project_id = os.environ["BOTTLENECK_PROJECT_ID"]
        location = os.environ["BOTTLENECK_LOCATION"]
        print(f"DEBUG: Calling Vertex AI endpoint: {endpoint_id} in {project_id}...")
        
        endpoint = aiplatform.Endpoint(
            endpoint_name=f"projects/{project_id}/locations/{location}/endpoints/{endpoint_id}"
        )
        prediction = endpoint.predict(
            instances=[{"prompt": user_prompt, "max_tokens": 512}]
        )
        
        print(f"DEBUG: Prediction received. {len(prediction.predictions)} items.")
        if not prediction.predictions:
            return "⚠️ No prediction returned from the model."
        
        raw_output = str(prediction.predictions[0])
    except Exception as e:
        print(f"DEBUG: Error calling Vertex AI: {e}")
        return f"⚠️ Error calling Bottleneck Detector endpoint: {e}"

    return _format_bottleneck_response(raw_output)


@tool
def agenda_generator(issue_number: int, risk_score: str, confidence: str, reasons: str) -> str:
    """Use when the user asks for an agenda or meeting bullets for an issue.
    You MUST provide the issue_number, and the risk_score, confidence, and reasons 
    previously obtained from the bottleneck_predictor tool. Do not guess these.
    """
    try:
        print(f"DEBUG: Fetching from Snowflake for issue #{issue_number} for Agenda...")
        row = btm._fetch_issue_row_by_where(
            "ISSUE_NUMBER = %s", (issue_number,),
            schema=btm.SF_SCHEMA, table=btm.SF_TABLE,
        )
    except KeyError as e:
        return f"⚠️ Issue not found in Snowflake for Agenda: {e}"
    except RuntimeError as e:
        return f"⚠️ Snowflake error for Agenda: {e}"

    user_prompt = btm.build_agenda_prompt(row, risk_score, confidence, reasons)

    try:
        endpoint_id = os.environ["AGENDA_ENDPOINT_ID"]
        project_id = os.environ["AGENDA_PROJECT_ID"]
        location = os.environ["AGENDA_LOCATION"]
        print(f"DEBUG: Calling Agenda Vertex AI endpoint: {endpoint_id} in {project_id}...")
        
        endpoint = aiplatform.Endpoint(
            endpoint_name=f"projects/{project_id}/locations/{location}/endpoints/{endpoint_id}"
        )
        prediction = endpoint.predict(
            instances=[{"prompt": user_prompt, "max_tokens": 1024}]
        )
        
        if not prediction.predictions:
            return "⚠️ No prediction returned from the Agenda model."
        
        raw_output = str(prediction.predictions[0])
    except Exception as e:
        print(f"DEBUG: Error calling Agenda endpoint: {e}")
        return f"⚠️ Error calling Agenda Generator endpoint: {e}"

    return raw_output


# ── Global Orchestration Setup ───────────────────────────────────────────────
# We initialize these globally to reuse connections/threads and avoid "interpreter shutdown" errors
print(f"BOOT: Initializing Global Orchestrator for {VERSION}")
GLOBAL_LLM = ChatVertexAI(model_name="gemini-2.0-flash-001", project=GCP_PROJECT_ID)

GLOBAL_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You are an intelligent Slackbot assistant. Answer user queries clearly. "
     "When a user asks about bottlenecks, stalled GitHub issues, blocked issues, "
     "or wants to score/check an issue number, use the bottleneck_predictor tool.\n"
     "After analyzing an issue with the bottleneck predictor, YOU MUST ALWAYS ASK: 'Would you like an agenda for this?'.\n"
     "If the user says yes or asks for an agenda, use the agenda_generator tool. "
     "Pass the issue_number and the exact risk score, confidence, and reasons you discovered from the previous bottleneck check."
     ),
    MessagesPlaceholder(variable_name="chat_history"),
    ("human", "{input}"),
    ("placeholder", "{agent_scratchpad}"),
])

GLOBAL_AGENT = create_tool_calling_agent(GLOBAL_LLM, [bottleneck_predictor, agenda_generator], GLOBAL_PROMPT)
GLOBAL_EXECUTOR = AgentExecutor(agent=GLOBAL_AGENT, tools=[bottleneck_predictor, agenda_generator], verbose=True)

def orchestrate(text: str, thread_ts: str = None) -> str:
    if not text or not text.strip():
        print(f"DEBUG [{VERSION}]: Received empty text for orchestration. Skipping.")
        return "I didn't catch that. Could you please rephrase?"

    try:
        session_id = thread_ts or "default"
        history = CHAT_HISTORY.get(session_id, [])
        
        print(f"DEBUG [{VERSION}]: Starting orchestration for: {text[:50]}... in thread: {session_id}")
        # Use the global executor
        result = GLOBAL_EXECUTOR.invoke({"input": text, "chat_history": history})
        
        history.append(HumanMessage(content=text))
        history.append(AIMessage(content=result["output"]))
        # Keep the last 10 messages (5 turns) to prevent context overflow
        CHAT_HISTORY[session_id] = history[-10:]
        
        print(f"DEBUG [{VERSION}]: Orchestration complete.")
        return result["output"]
    except Exception as e:
        import traceback
        print(f"DEBUG [{VERSION}]: Error in orchestrate: {e}")
        traceback.print_exc()
        return f"⚠️ Error: {e}"


# ── Slack Bolt app ─────────────────────────────────────────────────────────────
bolt_app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)


@bolt_app.command("/autobot")
def handle_command(ack, body):
    ack()

def handle_command_lazy(body, client):
    text = body.get("text", "")
    channel = body["channel_id"]
    user_id = body["user_id"]
    
    print(f"DEBUG [{VERSION}]: Command /autobot called by {user_id} with text: {text}")
    response = orchestrate(text)
    client.chat_postMessage(channel=channel, text=response)

bolt_app.command("/autobot")(ack=handle_command, lazy=[handle_command_lazy])


def handle_mention_ack(ack):
    ack()

def handle_mention_lazy(event, client, request):
    # Use Bolt's request object to check for retries in the lazy handler
    if request.headers.get("x-slack-retry-num"):
        print(f"DEBUG [{VERSION}]: Skipping retry for app_mention (lazy)")
        return

    text = event.get("text", "")
    channel = event["channel"]
    thread_ts = event.get("thread_ts", event["ts"])
    
    print(f"DEBUG [{VERSION}]: Processing app_mention in channel {channel}: {text[:50]}")
    # Use thread_ts for mentions to keep different threads separate
    response_text = orchestrate(text, thread_ts=thread_ts)
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=response_text,
    )

bolt_app.event("app_mention")(ack=handle_mention_ack, lazy=[handle_mention_lazy])


def handle_message_ack(ack):
    ack()

def handle_message_lazy(event, client, request):
    # Use Bolt's request object to check for retries in the lazy handler
    if request.headers.get("x-slack-retry-num"):
        print(f"DEBUG [{VERSION}]: Skipping retry for message (lazy)")
        return

    # Ignore bot messages
    if event.get("bot_id"):
        return
    
    # Only respond to DMs
    if event.get("channel_type") != "im":
        return

    text = event.get("text", "")
    # Fallback for some message event types
    if not text and "message" in event:
        text = event["message"].get("text", "")

    channel = event["channel"]
    # For DMs, use the channel ID as the session key so history is preserved across messages
    session_id = channel
    
    print(f"DEBUG [{VERSION}]: Processing message (DM) in channel {channel}: {text[:50] if text else 'EMPTY'}")
    response_text = orchestrate(text, thread_ts=session_id)
    client.chat_postMessage(
        channel=channel,
        text=response_text,
    )

bolt_app.event("message")(ack=handle_message_ack, lazy=[handle_message_lazy])


# ── DPO feedback: thumbs-down on bot replies → Postgres ───────────────────────
_CACHE_BOT_USER_ID = None


def _bot_user_id(client) -> str:
    uid = os.getenv("SLACK_BOT_USER_ID", "").strip()
    if uid:
        return uid
    global _CACHE_BOT_USER_ID
    if _CACHE_BOT_USER_ID is None:
        _CACHE_BOT_USER_ID = client.auth_test()["user_id"]
    return _CACHE_BOT_USER_ID


def _reaction_is_thumbs_down(name: str) -> bool:
    n = (name or "").strip().lower().replace(" ", "")
    return n in ("thumbsdown", "-1", "thumbs_down")


def handle_reaction_added_ack(ack):
    ack()


def handle_reaction_added_lazy(event, client, request):
    if request.headers.get("x-slack-retry-num"):
        print(f"DEBUG [{VERSION}]: Skipping retry for reaction_added (lazy)")
        return
    if os.getenv("DPO_FEEDBACK_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return
    if not dpo_feedback.database_url():
        return

    reaction_name = event.get("reaction") or ""
    if not _reaction_is_thumbs_down(reaction_name):
        return

    item = event.get("item") or {}
    if item.get("type") != "message":
        return

    channel_id = item.get("channel")
    message_ts = item.get("ts")
    reactor_user_id = event.get("user") or ""

    if not channel_id or not message_ts:
        print(f"DPO: reaction_added missing channel/ts: {event!r}")
        return

    bot_user_id = _bot_user_id(client)

    if os.getenv("DPO_FEEDBACK_ENSURE_SCHEMA", "").strip().lower() in ("1", "true", "yes"):
        try:
            dpo_feedback.ensure_schema()
        except Exception as e:
            print(f"DPO: ensure_schema failed: {e}")

    try:
        hist = client.conversations_history(
            channel=channel_id,
            latest=message_ts,
            oldest=message_ts,
            inclusive=True,
            limit=1,
        )
        hist_msgs = hist.get("messages") or []
        if not hist_msgs:
            print(f"DPO: conversations_history returned no rows for ts={message_ts}")
            return
        root = hist_msgs[0]
        thread_ts = root.get("thread_ts") or root.get("ts")
        rep = client.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
            limit=200,
        )
        thread_msgs = rep.get("messages") or []
        prompt_text, rejected_text, meta = dpo_feedback.resolve_prompt_and_rejected(
            thread_msgs,
            reacted_ts=message_ts,
            bot_user_id=bot_user_id,
        )
        meta["reaction"] = reaction_name
        meta["slack_channel"] = channel_id
        if not meta.get("rejected_from_bot_message"):
            print(
                f"DPO: skipping — reacted message ts={message_ts} is not a bot-authored reply"
            )
            return

        slack_team_id = event.get("team_id") or event.get("team")
        if isinstance(slack_team_id, dict):
            slack_team_id = slack_team_id.get("id")
        slack_team_id = slack_team_id or None

        if dpo_feedback.insert_feedback_event(
            slack_team_id=slack_team_id,
            channel_id=channel_id,
            message_ts=message_ts,
            reactor_user_id=reactor_user_id,
            prompt_text=prompt_text,
            rejected_text=rejected_text,
            metadata=meta,
            reaction=reaction_name or "thumbsdown",
        ):
            print(
                f"DPO: stored thumbs-down feedback "
                f"channel={channel_id} msg_ts={message_ts} reactor={reactor_user_id}"
            )
    except Exception as e:
        print(f"DPO: reaction_added handler failed: {e}")
        import traceback

        traceback.print_exc()


bolt_app.event("reaction_added")(
    ack=handle_reaction_added_ack,
    lazy=[handle_reaction_added_lazy],
)


# ── Flask server ───────────────────────────────────────────────────────────────
flask_app = Flask(__name__)
handler   = SlackRequestHandler(bolt_app)


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/slack/commands", methods=["POST"])
def slack_commands():
    return handler.handle(request)


@flask_app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    return handler.handle(request)


@flask_app.route("/health", methods=["GET"])
def health():
    return "ok", 200


if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
