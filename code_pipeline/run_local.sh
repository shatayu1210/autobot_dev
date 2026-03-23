#!/bin/bash

# Kill any existing processes on these ports
echo "Stopping any existing processes on ports 8000-8004..."
lsof -ti:8000,8001,8002,8003,8004 | xargs kill -9 2>/dev/null

# Set common environment variables for local development
export GOOGLE_CLOUD_PROJECT=$(gcloud config get-value project)
export GOOGLE_CLOUD_LOCATION="global"
export GOOGLE_GENAI_USE_VERTEXAI="True" # Use Gemini API locally
export GOOGLE_API_KEY="" # Use if not using Vertex AI

echo "Starting Planner Agent on port 8001..."
pushd agents/planner
uv run adk_app.py --host 0.0.0.0 --port 8001 --publish_agent_info --otel_to_cloud --a2a . &
PLANNER_PID=$!
popd

echo "Starting Critic Agent on port 8002..."
pushd agents/critic
uv run adk_app.py --host 0.0.0.0 --port 8002 --publish_agent_info --otel_to_cloud --a2a . &
CRITIC_PID=$!
popd

echo "Starting Patcher Agent on port 8003..."
pushd agents/patcher
uv run adk_app.py --host 0.0.0.0 --port 8003 --publish_agent_info --otel_to_cloud --a2a . &
PATCHER_PID=$!
popd

export PLANNER_AGENT_CARD_URL=http://localhost:8001/a2a/agent/.well-known/agent-card.json
export CRITIC_AGENT_CARD_URL=http://localhost:8002/a2a/agent/.well-known/agent-card.json
export PATCHER_AGENT_CARD_URL=http://localhost:8003/a2a/agent/.well-known/agent-card.json

echo "Starting Code Orchestrator Agent on port 8004..."
pushd agents/code_orchestrator
uv run adk_app.py --host 0.0.0.0 --port 8004 --publish_agent_info --otel_to_cloud --a2a . &
ORCHESTRATOR_PID=$!
popd

# Wait a bit for them to start up
sleep 5

echo "Starting App Server on port 8000..."
pushd app
export AGENT_SERVER_URL=http://localhost:8004

uv run uvicorn main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!
popd

echo "All agents started!"
echo "Planner: http://localhost:8001"
echo "Critic: http://localhost:8002"
echo "Patcher: http://localhost:8003"
echo "Code Orchestrator: http://localhost:8004"
echo "App Server (Frontend): http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop all agents."

# Wait for all processes
trap "kill $PLANNER_PID $CRITIC_PID $PATCHER_PID $ORCHESTRATOR_PID $BACKEND_PID; exit" INT
wait
