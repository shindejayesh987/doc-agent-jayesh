---
name: add-or-update-backend-api-endpoint
description: Workflow command scaffold for add-or-update-backend-api-endpoint in doc-agent-jayesh.
allowed_tools: ["Bash", "Read", "Write", "Grep", "Glob"]
---

# /add-or-update-backend-api-endpoint

Use this workflow when working on **add-or-update-backend-api-endpoint** in `doc-agent-jayesh`.

## Goal

Adds or updates a backend API endpoint, often with corresponding frontend API route and types, and sometimes with tests.

## Common Files

- `backend/routes/*.py`
- `backend/main.py`
- `frontend/app/api/*/route.ts`
- `frontend/lib/api.ts`
- `frontend/lib/types.ts`
- `tests/*.py`

## Suggested Sequence

1. Understand the current state and failure mode before editing.
2. Make the smallest coherent change that satisfies the workflow goal.
3. Run the most relevant verification for touched files.
4. Summarize what changed and what still needs review.

## Typical Commit Signals

- Edit or add backend/routes/*.py to implement the API logic.
- Update backend/main.py to register the route if needed.
- Update or add frontend/app/api/*/route.ts to proxy or call the backend API.
- Update frontend/lib/api.ts and frontend/lib/types.ts for API typing and client usage.
- Optionally, add or update tests in tests/ or scripts/.

## Notes

- Treat this as a scaffold, not a hard-coded script.
- Update the command if the workflow evolves materially.