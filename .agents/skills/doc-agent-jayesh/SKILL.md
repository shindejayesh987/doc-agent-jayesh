```markdown
# doc-agent-jayesh Development Patterns

> Auto-generated skill from repository analysis

## Overview

This skill teaches you how to contribute to the `doc-agent-jayesh` codebase, a Python project (with frontend and backend components) focused on document agent workflows. You'll learn the project's coding conventions, how to add new features or integrations, and how to follow established workflows for API endpoints, UI components, database schema changes, authentication, CI/CD automation, and LLM provider integrations.

## Coding Conventions

**File Naming**
- Python files: Use `camelCase` (e.g., `supabaseClient.py`, `main.py`)
- TypeScript/TSX files: Also use `camelCase` or PascalCase for components (e.g., `UserMenu.tsx`)

**Import Style**
- Use aliases for imports.
    ```python
    import backend.services.userService as userService
    ```
    ```typescript
    import * as api from '../lib/api'
    ```

**Export Style**
- Use named exports.
    ```python
    def get_user(...):
        ...
    ```
    ```typescript
    export function fetchData() { ... }
    ```

**Commit Patterns**
- Freeform commit messages, average length ~27 characters.
- No strict prefixing required.

## Workflows

### Add or Update Backend API Endpoint
**Trigger:** When you want to add a new backend API feature or modify an existing one  
**Command:** `/new-api-endpoint`

1. Edit or add files in `backend/routes/*.py` to implement API logic.
2. Update `backend/main.py` to register the new or updated route if needed.
3. Update or add `frontend/app/api/*/route.ts` to proxy or call the backend API.
4. Update `frontend/lib/api.ts` and `frontend/lib/types.ts` for API typing and client usage.
5. Optionally, add or update tests in `tests/` or `scripts/`.

**Example:**
```python
# backend/routes/userRoute.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/users")
def list_users():
    return [{"id": 1, "name": "Alice"}]
```
```typescript
// frontend/lib/api.ts
export async function fetchUsers() {
  return fetch('/api/users').then(res => res.json());
}
```

---

### Add or Update Frontend Component or UI Flow
**Trigger:** When you want to add a new UI feature or change user interaction  
**Command:** `/new-ui-feature`

1. Edit or add `frontend/components/*.tsx` for UI changes.
2. Update `frontend/app/*.tsx` or `frontend/app/*/page.tsx` to wire up components.
3. Update `frontend/lib/types.ts` or `frontend/lib/api.ts` if new data/API calls are needed.
4. Optionally, update `frontend/app/layout.tsx` or `frontend/app/assistant.tsx` for global UI changes.

**Example:**
```tsx
// frontend/components/UserMenu.tsx
export function UserMenu({ user }) {
  return <div>Hello, {user.name}</div>;
}
```

---

### Add Database Table or Schema Migration
**Trigger:** When you want to add a new data model or change the database structure  
**Command:** `/new-table`

1. Edit `supabase/schema.sql` or `supabase/migrations/*.sql` to define new table/schema changes.
2. Update `backend/services/*.py` or `backend/routes/*.py` to use the new schema.
3. Optionally, update `storage/supabase_client.py` for new CRUD operations.
4. Update `frontend/lib/types.ts` if the frontend needs new types.

**Example:**
```sql
-- supabase/migrations/20240101_add_documents.sql
CREATE TABLE documents (
  id serial PRIMARY KEY,
  title text,
  content text
);
```

---

### Add or Update Authentication or Access Control
**Trigger:** When you want to change authentication logic or access control  
**Command:** `/update-auth`

1. Edit `backend/auth.py` and `backend/main.py` for backend auth logic.
2. Edit `frontend/app/auth/callback/route.ts` and `frontend/middleware.ts` for frontend auth/session handling.
3. Update `frontend/.env.example`, `cloudbuild.yaml`, or `deploy.sh` for new environment variables.
4. Update `frontend/lib/supabase-browser.ts` or `frontend/lib/supabase-server.ts` if Supabase logic changes.
5. Update `frontend/components/AuthProvider.tsx` or `UserMenu.tsx` for UI changes.

**Example:**
```python
# backend/auth.py
def verify_jwt(token):
    ...
```
```tsx
// frontend/components/AuthProvider.tsx
export function AuthProvider({ children }) {
  // Provide user context
  return <AuthContext.Provider value={user}>{children}</AuthContext.Provider>;
}
```

---

### Add or Update GitHub Actions Workflow
**Trigger:** When you want to automate repository tasks via GitHub Actions  
**Command:** `/new-gh-action`

1. Edit or add `.github/workflows/*.yml` to define/update the workflow.
2. Optionally, add or update scripts in `scripts/` used by the workflow.
3. Optionally, update `.claude/commands/*.md` for command documentation.
4. Update `README.md` if user-facing automation changes.

**Example:**
```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - run: pip install -r requirements.txt
      - run: pytest
```

---

### Add Provider or LLM Integration
**Trigger:** When you want to support a new LLM provider or update provider-specific logic  
**Command:** `/add-provider`

1. Add or update `pageindex/llm/*_provider.py` for the new provider.
2. Update `pageindex/llm/factory.py` to register the provider.
3. Update `requirements.txt` to add SDK dependencies.
4. Update `app.py` or `backend/services/*` for provider-specific logic.
5. Optionally, update `frontend/lib/types.ts` or `frontend/lib/api.ts` for provider options.

**Example:**
```python
# pageindex/llm/openai_provider.py
class OpenAIProvider:
    def generate(self, prompt):
        ...
```
```python
# pageindex/llm/factory.py
from .openai_provider import OpenAIProvider

providers = {
    "openai": OpenAIProvider,
}
```

## Testing Patterns

- Test framework: Unknown (no standard detected).
- Test files may use the pattern `*.test.ts` for frontend, and Python tests are in `tests/*.py`.
- To add tests, create files matching these patterns in the appropriate directory.

**Example:**
```python
# tests/test_user.py
def test_list_users():
    ...
```
```typescript
// frontend/app/api/users.test.ts
import { fetchUsers } from '../../lib/api';
test('fetchUsers returns users', async () => {
  const users = await fetchUsers();
  expect(users).toBeDefined();
});
```

## Commands

| Command            | Purpose                                                |
|--------------------|--------------------------------------------------------|
| /new-api-endpoint  | Add or update a backend API endpoint                   |
| /new-ui-feature    | Add or update a frontend component or UI flow          |
| /new-table         | Add a new database table or schema migration           |
| /update-auth       | Add or update authentication or access control         |
| /new-gh-action     | Add or update a GitHub Actions workflow                |
| /add-provider      | Add a new LLM provider or update provider integration  |
```
