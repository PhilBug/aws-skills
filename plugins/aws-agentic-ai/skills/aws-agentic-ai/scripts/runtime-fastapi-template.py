#!/usr/bin/env python3
"""AgentCore HTTP Runtime Template (FastAPI).

A minimal FastAPI-based AgentCore Runtime with:
- /invocations endpoint (SSE streaming)
- /ping health check
- MCPClient lifecycle management (startup/shutdown)
- Per-request Agent creation

Usage:
    # Local development
    uvicorn runtime-fastapi-template:app --host 0.0.0.0 --port 8080 --reload

    # Deploy to AgentCore via CDK (see agentcore-runtime-deploy.md)
"""

import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

# --- Configuration ---
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "You are a helpful AI assistant.")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


# --- Request Models ---
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


# --- Module-level singletons (created once, reused across requests) ---
model = BedrockModel(model_id=MODEL_ID, region_name=AWS_REGION)
_mcp_client: MCPClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Initialize MCPClient at startup, close on shutdown."""
    global _mcp_client
    if MCP_SERVER_URL:
        from mcp.client.streamable_http import streamable_http_client

        _mcp_client = MCPClient(lambda: streamable_http_client(url=MCP_SERVER_URL))
    yield
    if _mcp_client:
        _mcp_client.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/ping")
def ping():
    return {"status": "Healthy"}


@app.post("/invocations")
async def invocations(request: ChatRequest):
    """Create a per-request Agent and stream SSE response."""
    user_message = request.messages[-1].content if request.messages else ""

    tools: list = [_mcp_client] if _mcp_client else []
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
