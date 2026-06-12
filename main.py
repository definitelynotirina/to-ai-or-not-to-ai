import asyncio
import os
from typing import Optional
from urllib.parse import urlparse

from agent_framework import AgentSession
from agent_framework.foundry import FoundryAgent
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import AgentDetails, AgentVersionDetails, AgentVersionStatus
from azure.core.exceptions import HttpResponseError
from azure.identity.aio import AzureCliCredential
from client_memory import get_client_profile, update_client_profile
from dotenv import load_dotenv


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def normalize_project_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(
            "AZURE_FOUNDRY_PROJECT_ENDPOINT must be a valid HTTPS URL."
        )

    marker = "/agents/"
    if marker in parsed.path:
        project_path = parsed.path.split(marker, 1)[0]
        return f"{parsed.scheme}://{parsed.netloc}{project_path}"

    return endpoint.rstrip("/")


def extract_agent_name_from_endpoint(endpoint: str) -> Optional[str]:
    parsed = urlparse(endpoint)
    parts = [part for part in parsed.path.split("/") if part]

    try:
        agents_index = parts.index("agents")
    except ValueError:
        return None

    if agents_index + 1 >= len(parts):
        return None

    return parts[agents_index + 1]


async def resolve_agent(
    project_client: AIProjectClient, agent_id: str, agent_name_hint: Optional[str] = None
) -> AgentDetails:
    async for agent in project_client.agents.list():
        latest_version = agent.versions.latest
        candidate_ids = {
            agent.id,
            getattr(latest_version, "id", None),
            getattr(latest_version, "agent_guid", None),
        }

        if agent_id in candidate_ids:
            return agent

        if agent_name_hint and agent.name == agent_name_hint:
            return agent

    if agent_name_hint:
        try:
            return await project_client.agents.get(agent_name_hint)
        except Exception:
            pass

    raise RuntimeError(
        "Could not find the configured Azure AI Foundry agent using the provided ID or endpoint name."
    )


async def resolve_ready_agent_version(
    project_client: AIProjectClient, agent_name: str
) -> AgentVersionDetails:
    versions: list[AgentVersionDetails] = []

    async for version in project_client.agents.list_versions(agent_name):
        versions.append(version)

    if not versions:
        raise RuntimeError(f"No versions were found for agent '{agent_name}'.")

    for version in versions:
        if version.status == AgentVersionStatus.ACTIVE:
            return version

    latest_version = versions[0]
    raise RuntimeError(
        f"No active version was found for agent '{agent_name}'. "
        f"Latest visible version is '{latest_version.version}' with status '{latest_version.status}'."
    )


async def chat_loop(agent: FoundryAgent, session: AgentSession) -> None:
    print("Connected to Azure AI Foundry agent. Type 'exit' or 'quit' to stop.")

    while True:
        user_input = input("\nYou: ").strip()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break

        print("Agent: ", end="", flush=True)
        response_started = False

        async for chunk in agent.run(user_input, session=session, stream=True):
            if chunk.text:
                print(chunk.text, end="", flush=True)
                response_started = True

        if not response_started:
            print("[No text response returned]", end="", flush=True)

        print()


async def create_conversation_session(agent: FoundryAgent) -> AgentSession:
    max_attempts = 10

    for attempt in range(1, max_attempts + 1):
        try:
            return agent.create_session()
        except HttpResponseError as exc:
            if exc.error and exc.error.code == "agent_version_not_ready" and attempt < max_attempts:
                wait_seconds = min(attempt * 3, 15)
                print(
                    f"Agent version is still provisioning. Retrying in {wait_seconds} seconds..."
                )
                await asyncio.sleep(wait_seconds)
                continue
            if exc.error and exc.error.code == "agent_version_not_ready":
                raise RuntimeError(
                    "Azure AI Foundry reports that the agent version is still provisioning. "
                    f"Agent '{getattr(agent, 'name', 'unknown')}' is not ready yet. "
                    "Wait for the agent deployment/version to finish in the Foundry portal, "
                    "then run the app again."
                ) from exc
            raise


def format_profile_context(profile: dict[str, object]) -> str:
    if not profile.get("found"):
        return (
            "No stored client memory exists yet for this client. "
            "Start fresh, but note any scoping patterns worth saving later."
        )

    return (
        "Known client profile:\n"
        f"- Client name: {profile.get('client_name', 'Unknown')}\n"
        f"- Past projects: {', '.join(profile.get('past_projects', [])) or 'None'}\n"
        f"- Observed patterns: {', '.join(profile.get('observed_patterns', [])) or 'None'}\n"
        f"- Preferred tools: {', '.join(profile.get('preferred_tools', [])) or 'None'}\n"
        f"- Avoided tools: {', '.join(profile.get('avoided_tools', [])) or 'None'}\n"
        "Use this memory when making scoping recommendations. "
        "For example, if the client frequently requests scope changes, prefer solutions with more customization headroom."
    )


async def stream_agent_reply(
    agent: FoundryAgent, session: AgentSession, message: str
) -> str:
    print("Agent: ", end="", flush=True)
    response_text_parts: list[str] = []

    async for chunk in agent.run(message, session=session, stream=True):
        if chunk.text:
            print(chunk.text, end="", flush=True)
            response_text_parts.append(chunk.text)

    if not response_text_parts:
        print("[No text response returned]", end="", flush=True)
        return "[No text response returned]"

    return "".join(response_text_parts)


def infer_session_observations(transcript: list[str]) -> dict[str, list[str]]:
    combined = " ".join(transcript).lower()
    observations: dict[str, list[str]] = {
        "past_projects": [],
        "observed_patterns": [],
        "preferred_tools": [],
        "avoided_tools": [],
        "session_observations": [],
    }

    if "scope change" in combined or "out of scope" in combined:
        observations["observed_patterns"].append("frequently requests out-of-scope features")
    if "budget" in combined or "cheap" in combined or "low cost" in combined:
        observations["observed_patterns"].append("has tight budgets")
    if "low-code" in combined or "low code" in combined:
        observations["observed_patterns"].append("prefers low-code solutions")
        observations["preferred_tools"].append("Copilot Studio")
    if "custom" in combined or "flexib" in combined:
        observations["preferred_tools"].append("Azure AI Foundry")
    if "avoid copilot studio" in combined:
        observations["avoided_tools"].append("Copilot Studio")
    if "avoid azure ai foundry" in combined:
        observations["avoided_tools"].append("Azure AI Foundry")
    if "word" in combined:
        observations["preferred_tools"].append("Work IQ Word tool")
    if "mcp" in combined or "learn docs" in combined:
        observations["preferred_tools"].append("Microsoft Learn MCP Server")

    for line in transcript[-6:]:
        cleaned = line.strip()
        if cleaned:
            observations["session_observations"].append(cleaned[:240])

    return observations


async def main() -> None:
    load_dotenv()

    raw_project_endpoint = require_env("AZURE_FOUNDRY_PROJECT_ENDPOINT")
    project_endpoint = normalize_project_endpoint(raw_project_endpoint)
    agent_id = require_env("AZURE_FOUNDRY_AGENT_ID")
    agent_name_hint = extract_agent_name_from_endpoint(raw_project_endpoint)

    credential = AzureCliCredential()
    project_client = AIProjectClient(
        endpoint=project_endpoint,
        credential=credential,
        allow_preview=True,
    )

    try:
        async with project_client:
            agent_details = await resolve_agent(
                project_client, agent_id, agent_name_hint=agent_name_hint
            )
            agent_name = agent_details.name
            ready_version = await resolve_ready_agent_version(project_client, agent_name)
            agent_version = ready_version.version

            print(
                "Resolved Foundry agent:",
                f"name={agent_name}",
                f"agent_id={agent_details.id}",
                f"version={agent_version}",
                f"status={ready_version.status}",
            )

            async with FoundryAgent(
                project_client=project_client,
                agent_name=agent_name,
                agent_version=agent_version,
                allow_preview=True,
            ) as agent:
                agent_session = await create_conversation_session(agent)
                transcript: list[str] = []
                client_name: Optional[str] = None

                greeting = await stream_agent_reply(
                    agent,
                    agent_session,
                    (
                        "Start this scoping conversation with a short greeting and invite the user "
                        "to describe what they want to build. Do not ask for client history yet."
                    ),
                )
                transcript.append(f"Agent: {greeting}")
                print()

                first_user_message = input("\nYou: ").strip()
                while not first_user_message:
                    first_user_message = input("\nYou: ").strip()

                transcript.append(f"User: {first_user_message}")
                first_response = await stream_agent_reply(
                    agent,
                    agent_session,
                    (
                        f"User request: {first_user_message}\n"
                        "Acknowledge the request briefly, then ask exactly: "
                        "\"Is this for an existing client or a new one?\""
                    ),
                )
                transcript.append(f"Agent: {first_response}")
                print()

                client_reply = input("\nYou: ").strip()
                while not client_reply:
                    client_reply = input("\nYou: ").strip()

                transcript.append(f"User: {client_reply}")

                if client_reply.strip().lower() == "new":
                    client_name = "New client"
                    profile = {
                        "client_name": client_name,
                        "found": False,
                        "message": "No client profile found yet.",
                    }
                else:
                    client_name = client_reply.strip()
                    profile = get_client_profile(client_name)

                print(
                    "Loaded client memory:",
                    "found existing profile" if profile.get("found") else "no existing profile",
                )

                memory_injection = (
                    "Client memory context for this scoping conversation:\n"
                    f"Client name: {client_name}\n"
                    f"{format_profile_context(profile)}\n"
                    "Continue the scoping conversation using this context."
                )
                transcript.append(f"System: {memory_injection}")

                memory_response = await stream_agent_reply(
                    agent,
                    agent_session,
                    memory_injection,
                )
                transcript.append(f"Agent: {memory_response}")
                print()

                while True:
                    user_input = input("\nYou: ").strip()
                    if not user_input:
                        continue
                    if user_input.lower() in {"exit", "quit"}:
                        break

                    transcript.append(f"User: {user_input}")
                    response_text = await stream_agent_reply(agent, agent_session, user_input)
                    transcript.append(f"Agent: {response_text}")
                    print()

                new_observations = infer_session_observations(transcript)
                updated_profile = update_client_profile(
                    client_name or "New client",
                    past_projects=new_observations["past_projects"],
                    observed_patterns=new_observations["observed_patterns"],
                    preferred_tools=new_observations["preferred_tools"],
                    avoided_tools=new_observations["avoided_tools"],
                    session_observations=new_observations["session_observations"],
                )

                print(
                    "\nSaved client memory for",
                    updated_profile["client_name"],
                    f"with {len(updated_profile.get('session_observations', []))} stored observations.",
                )
    finally:
        await credential.close()


if __name__ == "__main__":
    asyncio.run(main())
