# Adhoc Agent with Memory (LangGraph Checkpointers) Implementation Plan

## Objective
Upgrade the current multi-pass `llm_adhoc_query` function into a fully persistent, stateful ReAct agent using **LangGraph** and its **Checkpointer** architecture. This allows the user to have continuous, contextual conversations with the bot (e.g., "Who closed PR 123?" -> "What files did they change?"), where the agent retains the history of previously called tools, retrieved API data, and the conversational context without needing to manually shove raw API JSON into the prompt every time.

## 1. State Definition
**File Target**: `autobot_vscode/local_orchestrator/adhoc_graph.py` (New File)
- **Action**: Define the `AdhocState` class. In LangGraph, to maintain a conversation, the state must extend the standard `MessagesState`.
  ```python
  from typing import Annotated
  from typing_extensions import TypedDict
  from langgraph.graph.message import add_messages

  class AdhocState(TypedDict):
      messages: Annotated[list, add_messages]
  ```

## 2. Tool Registry Integration
**File Target**: `autobot_vscode/local_orchestrator/adhoc_graph.py`
- **Action**: Convert the existing `GITHUB_TOOLS` dictionary from `app.py` into LangChain `@tool` decorated functions.
- **Action**: Bind the tools to the LLM backend (e.g., `llm_with_tools = llm.bind_tools(tools)`).
- **Available Tools to Bind**:
  - `gh_get_issue`
  - `gh_get_pr_files`
  - `gh_search_issues`
  - `graphrag_similar_issues` (If Neo4j is available)
  - `graphrag_linked_prs` (If Neo4j is available)

## 3. The LangGraph ReAct Architecture
**File Target**: `autobot_vscode/local_orchestrator/adhoc_graph.py`
- **Nodes**:
  1. `agent_node(state)`: Invokes the tool-bound LLM with the current list of `messages` (the chat history + tool outputs).
  2. `tools_node(state)`: Uses LangGraph's pre-built `ToolNode(tools)` to execute the specific Python functions requested by the agent, capturing the API output and appending it to `messages` as a `ToolMessage`.
- **Conditional Edges**:
  - `agent_node` -> `should_continue` router.
    - If the LLM response contains `tool_calls`, route to `tools_node`.
    - If the LLM response is just text (answer finalized), route to `END`.
  - `tools_node` -> always routes back to `agent_node` to let the LLM read the API output and summarize.

## 4. Persistent Memory (Checkpointers)
**File Target**: `autobot_vscode/local_orchestrator/adhoc_graph.py`
- **Action**: Implement an `In-Memory SqliteSaver` or `MemorySaver` to persist the graph state across HTTP requests.
  ```python
  from langgraph.checkpoint.memory import MemorySaver
  memory = MemorySaver()
  
  graph = workflow.compile(checkpointer=memory)
  ```
- **Execution Workflow**:
  - When the UI sends an adhoc query, it must pass a `thread_id` (e.g., a session UUID generated when VS Code starts).
  - The orchestrator invokes the graph: `graph.invoke({"messages": [HumanMessage(content=query)]}, config={"configurable": {"thread_id": "vscode-session-1"}})`
  - Because of the checkpointer, LangGraph automatically retrieves the previous `messages` array from memory, appends the new question, and passes the entire rich context to the LLM.

## 5. UI Integration
**File Target**: `autobot_vscode/media/webview.js` & `app.py`
- **Action**: Generate a unique `sessionId` in `webview.js` on load, and pass it with every `/api/orchestrate_stream` query.
- **Action**: Modify `app.py`'s `query` endpoint to accept `thread_id` and pass it to the compiled LangGraph checkpointer configuration.
- **Result**: The agent now behaves like a true chatbot. Users can say *"look up issue 400"*, followed by *"who commented on it?"*, and the agent will intrinsically know "it" refers to issue 400 without requiring the user to restate the context.
