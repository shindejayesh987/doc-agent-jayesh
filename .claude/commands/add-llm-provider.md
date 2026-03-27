---
name: add-llm-provider
description: Workflow command scaffold for add-llm-provider in doc-agent-jayesh.
allowed_tools: ["Bash", "Read", "Write", "Grep", "Glob"]
---

# /add-llm-provider

Use this workflow when working on **add-llm-provider** in `doc-agent-jayesh`.

## Goal

Add a new LLM provider to the pipeline, including integration, configuration, and UI support.

## Common Files

- `pageindex/llm/factory.py`
- `pageindex/llm/{provider}_provider.py`
- `requirements.txt`
- `app.py`
- `backend/main.py`

## Suggested Sequence

1. Understand the current state and failure mode before editing.
2. Make the smallest coherent change that satisfies the workflow goal.
3. Run the most relevant verification for touched files.
4. Summarize what changed and what still needs review.

## Typical Commit Signals

- Create or update provider implementation file (e.g., pageindex/llm/{provider}_provider.py)
- Update provider factory (pageindex/llm/factory.py) to register new provider
- Update requirements.txt with new dependencies if needed
- Update app.py or backend/main.py to expose provider in UI/configuration
- Update or add tests for provider integration

## Notes

- Treat this as a scaffold, not a hard-coded script.
- Update the command if the workflow evolves materially.