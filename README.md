# Azure AI Foundry Chat App

Simple Python terminal chat application built with the Microsoft Agent Framework (MAF) and connected to an existing Azure AI Foundry agent.

## What it does

- Authenticates with Azure using `az login`
- Loads `AZURE_FOUNDRY_PROJECT_ENDPOINT` and `AZURE_FOUNDRY_AGENT_ID` from a `.env` file
- Resolves the configured Foundry agent
- Starts a multi-turn terminal chat session and prints streamed agent responses

## Setup

1. Sign in with Azure CLI:

```bash
az login
```

2. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

3. Create a `.env` file from the example and fill in your project values:

```bash
copy .env.example .env
```

If you only have the agent-specific Responses URL, that is also fine. The app will trim it back to the project endpoint automatically.

4. Run the app:

```bash
python main.py
```

Type `exit` or `quit` to end the chat session.

## Architecture overview

The terminal client uses Microsoft Agent Framework's `FoundryAgent` to connect to an existing Azure AI Foundry agent. The Foundry agent can draw on a Foundry IQ knowledge base backed by a past projects dataset, call an MCP Server connected to Microsoft Learn documentation, and use a Work IQ Word tool to generate Word documents. The same agent experience is published to M365 Copilot Chat.
