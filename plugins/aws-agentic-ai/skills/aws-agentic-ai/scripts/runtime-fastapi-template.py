#!/usr/bin/env python3
"""AgentCore HTTP Runtime Template (FastAPI).

A minimal FastAPI-based AgentCore Runtime with:
- /invocations endpoint (SSE streaming)
- /ping health check with async task tracking
- MCPClient lifecycle management (startup/shutdown)
- Per-request Agent with session_manager

Usage:
    # Local development
    uvicorn runtime-fastapi-template:app --host 0.0.0.0 --port 8080 --reload

    # Deploy to AgentCore via CDK (see agentcore-runtime-deploy.md)
"""

import json
import os
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from strands import Agent
from strands.tools.mcp import MCPClient

# --- Configuration ---
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "You are a helpful AI assistant.")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


# --- Request/Response Models ---
class MessagePart(BaseModel):
    type: str = "text"
    text: str


class Message(BaseModel):
    id: str = ""
    role: str
    content: str
    parts: list[MessagePart] = []


class ChatRequest(BaseModel):
    id: str  # Session ID
    user_id: str = ""
    messages: list[Message] = []


# --- App Setup ---
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Async Task Tracking ---
_active_tasks: set[int] = set()
_task_lock = threading.Lock()
_task_counter = 0

# --- MCP Client (process-level, created at startup) ---
_mcp_client: MCPClient | None = None


def add_task() -> int:
    """Register an async background task. /ping returns HealthyBusy while tasks are active."""
    global _task_counter
    with _task_lock:
        _task_counter += 1
        _active_tasks.add(_task_counter)
        return _task_counter


def complete_task(task_id: int):
    """Mark an async task as complete."""
    with _task_lock:
        _active_tasks.discard(task_id)


@app.on_event("startup")
async def startup():
    """Initialize MCPClient at startup (lazy connection on first tool call)."""
    global _mcp_client
    if MCP_SERVER_URL:
        from mcp.client.streamable_http import streamable_http_client

        _mcp_client = MCPClient(lambda: streamable_http_client(url=MCP_SERVER_URL))


@app.on_event("shutdown")
async def shutdown():
    """Close MCPClient on process shutdown."""
    if _mcp_client:
        await _mcp_client.close()


@app.get("/ping")
def ping():
    """Health check. Returns HealthyBusy if async tasks are running."""
    status = "HealthyBusy" if _active_tasks else "Healthy"
    return {"status": status}


@app.post("/invocations")
async def invocations(request: ChatRequest):
    """Main chat endpoint. Creates a per-request Agent and streams SSE response."""
    user_message = ""
    if request.messages:
        user_message = request.messages[-1].content

    # Build tool list
    tools = []
    if _mcp_client:
        tools.append(_mcp_client)

    # Create per-request Agent
    from strands.models.bedrock import BedrockModel

    model = BedrockModel(model_id=MODEL_ID, region_name=AWS_REGION)
    agent = Agent(
        system_prompt=SYSTEM_PROMPT,
        model=model,
        tools=tools,
    )

    async def event_generator():
        yield f"data: {json.dumps({'type': 'start', 'session_id': request.id})}\n\n"
        try:
            async for event in agent.stream_async(user_message):
                if "data" in event:
                    text = event.get("data", "")
                    if text:
                        yield f"data: {json.dumps({'type': 'text-delta', 'delta': text})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        yield f"data: {json.dumps({'type': 'finish', 'session_id': request.id})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
