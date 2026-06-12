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
                await chat_loop(agent, agent_session)
    finally:
        await credential.close()


if __name__ == "__main__":
    asyncio.run(main())
