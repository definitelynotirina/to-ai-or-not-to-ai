import asyncio
import os
import re
import uuid
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Timer
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_framework import AgentSession
from agent_framework.foundry import FoundryAgent
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import AzureCliCredential
from client_memory import get_client_profile, update_client_profile
from dotenv import load_dotenv
from main import (
    create_conversation_session,
    extract_agent_name_from_endpoint,
    format_profile_context,
    infer_session_observations,
    normalize_project_endpoint,
    require_env,
    resolve_agent,
    resolve_ready_agent_version,
)


STATIC_DIR = Path(__file__).with_name("static")
SUPERSCRIPT_DIGITS = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


class ChatMessageRequest(BaseModel):
    session_id: str
    message: str


class EndSessionRequest(BaseModel):
    session_id: str


@dataclass
class ConversationState:
    session: AgentSession
    transcript: list[str] = field(default_factory=list)
    stage: str = "awaiting_first_message"
    client_name: Optional[str] = None
    ended: bool = False


class ChatRuntime:
    def __init__(self) -> None:
        self.credential: Optional[AzureCliCredential] = None
        self.project_client: Optional[AIProjectClient] = None
        self.agent: Optional[FoundryAgent] = None
        self._agent_context = None
        self.sessions: dict[str, ConversationState] = {}
        self.lock = asyncio.Lock()

    async def startup(self) -> None:
        load_dotenv()

        raw_project_endpoint = require_env("AZURE_FOUNDRY_PROJECT_ENDPOINT")
        project_endpoint = normalize_project_endpoint(raw_project_endpoint)
        agent_id = require_env("AZURE_FOUNDRY_AGENT_ID")
        agent_name_hint = extract_agent_name_from_endpoint(raw_project_endpoint)

        self.credential = AzureCliCredential()
        self.project_client = AIProjectClient(
            endpoint=project_endpoint,
            credential=self.credential,
            allow_preview=True,
        )

        await self.project_client.__aenter__()
        agent_details = await resolve_agent(
            self.project_client, agent_id, agent_name_hint=agent_name_hint
        )
        ready_version = await resolve_ready_agent_version(
            self.project_client, agent_details.name
        )

        self._agent_context = FoundryAgent(
            project_client=self.project_client,
            agent_name=agent_details.name,
            agent_version=ready_version.version,
            allow_preview=True,
        )
        self.agent = await self._agent_context.__aenter__()

        print(
            "Resolved Foundry agent:",
            f"name={agent_details.name}",
            f"agent_id={agent_details.id}",
            f"version={ready_version.version}",
            f"status={ready_version.status}",
        )

    async def shutdown(self) -> None:
        if self._agent_context is not None:
            await self._agent_context.__aexit__(None, None, None)
        if self.project_client is not None:
            await self.project_client.__aexit__(None, None, None)
        if self.credential is not None:
            await self.credential.close()

    async def start_session(self) -> dict[str, str]:
        if self.agent is None:
            raise RuntimeError("Agent runtime is not initialized.")

        agent_session = await create_conversation_session(self.agent)
        session_id = str(uuid.uuid4())
        greeting = await self._send(
            agent_session,
            (
                "Start this scoping conversation with a short greeting and invite the user "
                "to describe what they want to build. Do not ask for client history yet."
            ),
        )

        self.sessions[session_id] = ConversationState(
            session=agent_session,
            transcript=[f"Agent: {greeting}"],
        )
        return {"session_id": session_id, "message": greeting}

    async def handle_message(self, session_id: str, message: str) -> dict[str, object]:
        state = self.sessions.get(session_id)
        if state is None or state.ended:
            raise HTTPException(status_code=404, detail="Chat session not found.")

        user_message = message.strip()
        if not user_message:
            raise HTTPException(status_code=400, detail="Message cannot be empty.")

        if user_message.lower() in {"exit", "quit"}:
            final_message = await self.end_session(session_id)
            return {"message": final_message, "ended": True}

        if state.stage == "awaiting_first_message":
            state.transcript.append(f"User: {user_message}")
            reply = await self._send(
                state.session,
                (
                    f"User request: {user_message}\n"
                    "Acknowledge the request briefly, then ask exactly: "
                    "\"Is this for an existing client or a new one?\""
                ),
            )
            state.transcript.append(f"Agent: {reply}")
            state.stage = "awaiting_client_name"
            return {"message": reply, "ended": False}

        if state.stage == "awaiting_client_name":
            state.transcript.append(f"User: {user_message}")

            if user_message.lower() == "new":
                client_name = "New client"
                profile = {
                    "client_name": client_name,
                    "found": False,
                    "message": "No client profile found yet.",
                }
            else:
                client_name = user_message
                profile = get_client_profile(client_name)

            state.client_name = client_name
            memory_injection = (
                "Client memory context for this scoping conversation:\n"
                f"Client name: {client_name}\n"
                f"{format_profile_context(profile)}\n"
                "Continue the scoping conversation using this context."
            )
            state.transcript.append(f"System: {memory_injection}")

            reply = await self._send(state.session, memory_injection)
            state.transcript.append(f"Agent: {reply}")
            state.stage = "active"

            return {
                "message": reply,
                "ended": False,
                "memory_status": (
                    f"found existing profile for {profile.get('matched_client_name', client_name)}"
                )
                if profile.get("found")
                else "no existing profile",
            }

        state.transcript.append(f"User: {user_message}")
        reply = await self._send(state.session, user_message)
        state.transcript.append(f"Agent: {reply}")
        return {"message": reply, "ended": False}

    async def end_session(self, session_id: str) -> str:
        state = self.sessions.get(session_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Chat session not found.")
        if state.ended:
            return "Conversation already ended."

        new_observations = infer_session_observations(state.transcript)
        updated_profile = update_client_profile(
            state.client_name or "New client",
            past_projects=new_observations["past_projects"],
            observed_patterns=new_observations["observed_patterns"],
            preferred_tools=new_observations["preferred_tools"],
            avoided_tools=new_observations["avoided_tools"],
            session_observations=new_observations["session_observations"],
        )
        state.ended = True

        return (
            f"Conversation ended. Saved client memory for {updated_profile['client_name']}."
        )

    async def _send(self, session: AgentSession, message: str) -> str:
        if self.agent is None:
            raise RuntimeError("Agent runtime is not initialized.")

        response_parts: list[str] = []
        async for chunk in self.agent.run(message, session=session, stream=True):
            if chunk.text:
                response_parts.append(chunk.text)
        response_text = "".join(response_parts) or "[No text response returned]"
        return replace_citation_markers(response_text)


def replace_citation_markers(text: str) -> str:
    counter = 0

    def repl(_: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        return str(counter).translate(SUPERSCRIPT_DIGITS)

    return re.sub(r"【[^】]+†[^】]+】", repl, text)


runtime = ChatRuntime()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await runtime.startup()
    try:
        yield
    finally:
        await runtime.shutdown()


app = FastAPI(title="MAF Chat UI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/session/start")
async def start_session() -> dict[str, str]:
    return await runtime.start_session()


@app.post("/api/chat")
async def chat(request: ChatMessageRequest) -> dict[str, object]:
    return await runtime.handle_message(request.session_id, request.message)


@app.post("/api/session/end")
async def end_session(request: EndSessionRequest) -> dict[str, str]:
    message = await runtime.end_session(request.session_id)
    return {"message": message}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    url = f"http://127.0.0.1:{port}"
    Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Serving web UI at {url}")
    uvicorn.run(app, host="127.0.0.1", port=port)
