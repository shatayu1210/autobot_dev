import asyncio
import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(os.getenv("ENV_FILE", ".env"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Import the compiled LangGraph pipeline
from pipeline.graph import graph

app = FastAPI(title="AutoBot LangGraph")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    user_id: str = "user"


@app.post("/api/chat_stream")
async def chat_stream(request: ChatRequest):
    """Stream pipeline progress as NDJSON."""

    message = request.message.strip()

    initial = {
        "issue_ref": message,
        "owner": "",
        "repo": "",
        "issue_number": 0,
        "issue_data": {},
        "patch_plan": "",
        "patch_diff": "",
        "verdict": "",
        "feedback": "",
        "iteration": 0,
        "max_iterations": 3,
        "final_result": "",
        "progress": [],
    }

    async def event_generator():
        HEARTBEAT_INTERVAL = 10  # seconds

        queue: asyncio.Queue = asyncio.Queue()

        async def _consume():
            """Run graph.astream and push events into queue."""
            try:
                async for chunk in graph.astream(initial, stream_mode="updates"):
                    await queue.put(chunk)
            except Exception as e:
                logger.error(f"[_consume] graph.astream error: {e}")
                await queue.put({"_error": str(e)})
            finally:
                await queue.put(None)  # sentinel

        consumer_task = asyncio.create_task(_consume())

        final_result = ""

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_INTERVAL
                    )
                except asyncio.TimeoutError:
                    # Pipeline still running — send keepalive
                    yield json.dumps({"type": "heartbeat"}) + "\n"
                    continue

                if chunk is None:
                    # Sentinel — pipeline finished
                    break

                if "_error" in chunk:
                    yield json.dumps(
                        {"type": "progress", "text": f"Pipeline error: {chunk['_error']}"}
                    ) + "\n"
                    continue

                # chunk format: {"node_name": {state_updates_dict}}
                for node_name, node_output in chunk.items():
                    if not isinstance(node_output, dict):
                        continue

                    # Stream progress messages
                    progress_msgs = node_output.get("progress", [])
                    for msg in progress_msgs:
                        logger.info(f"[{node_name}] {msg}")
                        yield json.dumps({"type": "progress", "text": msg}) + "\n"

                    # Capture final result when output node fires
                    if node_output.get("final_result"):
                        final_result = node_output["final_result"]

        finally:
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass

        if not final_result:
            final_result = "Pipeline completed but produced no output. Check server logs."

        logger.info(
            f"[event_generator] Sending result ({len(final_result)} chars)"
        )
        yield json.dumps({"type": "result", "text": final_result}) + "\n"

    return StreamingResponse(
        event_generator(),
        media_type="application/x-ndjson",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# Mount frontend static files
frontend_path = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
