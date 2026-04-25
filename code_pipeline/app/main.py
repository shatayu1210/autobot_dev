import logging
import os
import json
from typing import Any, AsyncGenerator, Dict, List, Optional

from dotenv import load_dotenv

import httpx
from httpx_sse import aconnect_sse

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google.genai import types as genai_types
from opentelemetry import trace
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.sdk.trace import TracerProvider, export
from pydantic import BaseModel

from traced_authenticated_httpx import create_traced_authenticated_client # type: ignore

class Feedback(BaseModel):
    score: float
    text: str | None = None
    run_id: str | None = None
    user_id: str | None = None

load_dotenv(os.getenv("ENV_FILE", ".env"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

provider = TracerProvider()
processor = export.BatchSpanProcessor(
    CloudTraceSpanExporter(project_id=os.getenv("GOOGLE_CLOUD_PROJECT"))
)
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)

app = FastAPI()

app.add_middleware(
    CORSMiddleware, # type: ignore
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent_name = os.getenv("AGENT_NAME", None)
agent_server_url = os.getenv("AGENT_SERVER_URL")
if not agent_server_url:
    raise ValueError("AGENT_SERVER_URL environment variable not set")
else:
    agent_server_url = agent_server_url.rstrip("/")

clients: Dict[str, httpx.AsyncClient] = {}

async def get_client(agent_server_origin: str) -> httpx.AsyncClient:
    global clients
    if agent_server_origin not in clients:
        clients[agent_server_origin] = create_traced_authenticated_client(agent_server_origin)
    return clients[agent_server_origin]

async def create_session(agent_server_origin: str, agent_name: str, user_id: str) -> Dict[str, Any]:
    httpx_client = await get_client(agent_server_origin)
    headers=[
        ("Content-Type", "application/json")
    ]
    session_request_url = f"{agent_server_origin}/apps/{agent_name}/users/{user_id}/sessions"
    session_response = await httpx_client.post(
        session_request_url,
        headers=headers
    )
    session_response.raise_for_status()
    return session_response.json()

async def get_session(agent_server_origin: str, agent_name: str, user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
    httpx_client = await get_client(agent_server_origin)
    headers=[
        ("Content-Type", "application/json")
    ]
    session_request_url = f"{agent_server_origin}/apps/{agent_name}/users/{user_id}/sessions/{session_id}"
    session_response = await httpx_client.get(
        session_request_url,
        headers=headers
    )
    if session_response.status_code == 404:
        return None
    session_response.raise_for_status()
    return session_response.json()


async def list_agents(agent_server_origin: str) -> List[str]:
    httpx_client = await get_client(agent_server_origin)
    headers=[
        ("Content-Type", "application/json")
    ]
    list_url = f"{agent_server_origin}/list-apps"
    list_response = await httpx_client.get(
        list_url,
        headers=headers
    )
    list_response.raise_for_status()
    agent_list = list_response.json()
    if not agent_list:
        agent_list = ["agent"]
    return agent_list


async def query_adk_sever(
        agent_server_origin: str, agent_name: str, user_id: str, message: str, session_id
) -> AsyncGenerator[Dict[str, Any], None]:
    httpx_client = await get_client(agent_server_origin)
    request = {
        "appName": agent_name,
        "userId": user_id,
        "sessionId": session_id,
        "newMessage": {
            "role": "user",
            "parts": [{"text": message}]
        },
        "streaming": False
    }
    async with aconnect_sse(
        httpx_client,
        "POST",
        f"{agent_server_origin}/run_sse",
        json=request
    ) as event_source:
        if event_source.response.is_error:
            event = {
                "author": agent_name,
                "content":{
                    "parts": [
                        {
                            "text": f"Error {event_source.response.text}"
                        }
                    ]
                }
            }
            yield event
        else:
            async for server_event in event_source.aiter_sse():
                event = server_event.json()
                yield event

class SimpleChatRequest(BaseModel):
    message: str
    user_id: str = "test_user"
    session_id: Optional[str] = None

@app.post("/api/chat_stream")
async def chat_stream(request: SimpleChatRequest):
    """Streaming chat endpoint."""
    global agent_name, agent_server_url
    if not agent_name:
        agent_name = (await list_agents(agent_server_url))[0] # type: ignore

    session = None
    if request.session_id:
        session = await get_session(
            agent_server_url, # type: ignore
            agent_name,
            request.user_id,
            request.session_id
        )
    if session is None:
        session = await create_session(
            agent_server_url, # type: ignore
            agent_name,
            request.user_id
        )

    events = query_adk_sever(
        agent_server_url, # type: ignore
        agent_name,
        request.user_id,
        request.message,
        session["id"]
    )

    async def event_generator():
        import asyncio

        patch_output_text = ""
        all_text_by_author: dict = {}
        HEARTBEAT_INTERVAL = 10  # seconds

        # Use a Queue + background task so the SSE connection to the orchestrator
        # is never cancelled by a timeout. The timeout only fires on queue.get(),
        # which lets us send keepalive chunks without disturbing the httpx stream.
        queue: asyncio.Queue = asyncio.Queue()

        async def _consume():
            try:
                async for event in events:
                    await queue.put(event)
            except Exception as e:
                logger.error(f"[_consume] SSE read error: {e}")
            finally:
                await queue.put(None)  # sentinel: pipeline finished

        consumer_task = asyncio.create_task(_consume())

        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    # No event arrived — pipeline still running; send keepalive
                    yield json.dumps({"type": "heartbeat"}) + "\n"
                    continue

                if event is None:  # sentinel
                    break

                author = event.get("author", "")
                logger.info(f"[event_generator] author={author!r} has_content={bool(event.get('content'))}")

                # Progress updates
                if author == "planner":
                    yield json.dumps({"type": "progress", "text": "Planner is analyzing the issue and building a patch plan..."}) + "\n"
                elif author == "patcher":
                    yield json.dumps({"type": "progress", "text": "Patcher is generating the unified diff..."}) + "\n"
                elif author == "critic":
                    yield json.dumps({"type": "progress", "text": "Critic is evaluating the diff..."}) + "\n"
                elif author == "verdict_checker":
                    yield json.dumps({"type": "progress", "text": "Checking verdict..."}) + "\n"
                elif author == "patch_output":
                    yield json.dumps({"type": "progress", "text": "Finalizing result..."}) + "\n"

                # Extract text content
                if "content" in event and event["content"]:
                    try:
                        content = genai_types.Content.model_validate(event["content"])
                        for part in content.parts or []:  # type: ignore
                            if part.text and part.text.strip():
                                all_text_by_author.setdefault(author, "")
                                all_text_by_author[author] += part.text
                                if author == "patch_output":
                                    patch_output_text += part.text
                    except Exception as e:
                        logger.warning(f"[event_generator] Could not parse content for author={author}: {e}")
        finally:
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass

        # Prefer patch_output's formatted result; fall back to last agent with text
        result_text = patch_output_text.strip()
        if not result_text and all_text_by_author:
            for a in reversed(list(all_text_by_author.keys())):
                candidate = all_text_by_author[a].strip()
                if candidate:
                    result_text = candidate
                    logger.warning(f"[event_generator] patch_output empty; using text from author={a!r}")
                    break

        if not result_text:
            result_text = "Pipeline completed but produced no output. Check server logs."

        logger.info(f"[event_generator] Sending result ({len(result_text)} chars), authors seen: {list(all_text_by_author.keys())}")
        yield json.dumps({"type": "result", "text": result_text}) + "\n"

    return StreamingResponse(
        event_generator(),
        media_type="application/x-ndjson",
        headers={
            "X-Accel-Buffering": "no",   # Disables nginx/Cloud Shell proxy buffering
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )

# Mount frontend from the copied location
frontend_path = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")

from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
app = OpenTelemetryMiddleware(app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
