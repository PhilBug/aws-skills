#!/usr/bin/env python3
"""AgentCore AG-UI Server Template.

Deploys as an AG-UI protocol Runtime on AgentCore.
Port 8080, endpoint /invocations, SSE event stream with AG-UI event types.

Note: The AG-UI ecosystem is rapidly evolving. Package names and APIs
may change. Refer to ag-ui.com and PyPI for latest versions.

Usage:
    # Local development
    pip install fastapi uvicorn ag-ui-strands
    python agui-server-template.py

    # Deploy to AgentCore
    agentcore configure -e agui-server-template.py --protocol AGUI
    agentcore deploy
"""

import uvicorn
from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from ag_ui_strands import StrandsAgent
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from strands import Agent
from strands.models.bedrock import BedrockModel

model = BedrockModel(
    model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    region_name="us-west-2",
)

strands_agent = Agent(
    model=model,
    system_prompt="You are a helpful assistant.",
)

agui_agent = StrandsAgent(
    agent=strands_agent,
    name="my_agent",
    description="A helpful assistant",
)

app = FastAPI()


@app.post("/invocations")
async def invocations(input_data: dict, request: Request):
    accept_header = request.headers.get("accept")
    encoder = EventEncoder(accept=accept_header)

    async def event_generator():
        run_input = RunAgentInput(**input_data)
        async for event in agui_agent.run(run_input):
            yield encoder.encode(event)

    return StreamingResponse(
        event_generator(),
        media_type=encoder.get_content_type(),
    )


@app.get("/ping")
async def ping():
    return JSONResponse({"status": "Healthy"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
