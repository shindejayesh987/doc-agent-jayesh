```markdown
# doc-agent-jayesh Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill provides a comprehensive guide to contributing to the `doc-agent-jayesh` codebase. It covers coding conventions, common development workflows, and best practices for extending, maintaining, and testing the project. The repository is primarily Python-based, with a focus on modular backend development, integration with Supabase, and a TypeScript/React frontend. No specific Python framework is enforced, and automation is handled via GitHub Actions.

## Coding Conventions

### File Naming
- **Python files:** Use `camelCase` (e.g., `supabaseClient.py`, `llmFactory.py`)
- **TypeScript/JSX files:** Use `PascalCase` for components (e.g., `DocumentList.tsx`), `camelCase` for utilities.

### Import Style
- **Alias imports** are preferred in Python:
  ```python
  import numpy as np
  import backend.services.userService as user_svc
  ```

### Export Style
- **Named exports** are used in TypeScript:
  ```typescript
  // frontend/lib/api.ts
  export function fetchDocuments() { ... }
  export const API_URL = '...';
  ```

### Commit Patterns
- Freeform commit messages (no strict prefix)
- Average commit message length: ~24 characters

## Workflows

### Add LLM Provider
**Trigger:** When you want to support a new LLM provider (e.g., Gemini, Ollama, OpenAI, Anthropic, etc.) in the app.  
**Command:** `/add-llm-provider`

1. Create or update the provider implementation file:  
   `pageindex/llm/{provider}_provider.py`
2. Register the new provider in the factory:  
   Edit `pageindex/llm/factory.py`
   ```python
   from .gemini_provider import GeminiProvider
   PROVIDERS['gemini'] = GeminiProvider
   ```
3. Add any new dependencies to `requirements.txt`.
4. Expose the provider in the UI/configuration:  
   Update `app.py` or `backend/main.py`.
5. Add or update tests for the provider integration.

---

### Add or Update Supabase Table or Schema
**Trigger:** When you need to add a new table, column, or policy to Supabase.  
**Command:** `/new-table`

1. Create or update a migration SQL file:  
   `supabase/migrations/{timestamp}_add_table.sql`
2. Update the main schema:  
   Edit `supabase/schema.sql`
3. Update backend code to use the new/changed schema:  
   - `backend/main.py`
   - `backend/routes/*.py`
   - `backend/services/*.py`
4. Update CRUD operations in:  
   `storage/supabase_client.py`
5. Update or add scripts for admin/bulk operations if needed.

---

### Feature Development with UI and Backend
**Trigger:** When adding a major feature spanning backend, frontend, and possibly scripts.  
**Command:** `/new-feature`

1. Implement backend logic:  
   - `backend/main.py`
   - `backend/routes/*.py`
   - `backend/services/*.py`
2. Implement or update frontend UI components:  
   - `frontend/components/*.tsx`
   - `frontend/app/*.ts[x]`
3. Update frontend API calls/types:  
   - `frontend/lib/api.ts`
   - `frontend/lib/types.ts`
4. Update or add scripts for admin/batch operations:  
   `scripts/*.py`
5. Update database schema/migrations if persistence is needed:  
   - `supabase/migrations/*.sql`
   - `supabase/schema.sql`
6. Add or update tests if applicable.

---

### Add or Update GitHub Actions Automation
**Trigger:** When automating repo management tasks via GitHub Actions.  
**Command:** `/new-gh-action`

1. Create or update workflow YAML files:  
   `.github/workflows/*.yml`
2. Add or update supporting scripts:  
   - `scripts/*.js`
   - `scripts/*.sh`
3. Add or update documentation for commands:  
   `.claude/commands/*.md`
4. Test the workflow by running or backfilling on the repo.

---

### Improve Error Handling and User Feedback
**Trigger:** When making errors more understandable or improving progress reporting.  
**Command:** `/improve-errors`

1. Update app logic to catch and map exceptions to friendly messages:  
   - `app.py`
   - `backend/main.py`
   - `backend/routes/*.py`
   ```python
   try:
       # risky operation
   except ValueError as e:
       return {"error": "Invalid input. Please check your data."}
   ```
2. Add or improve progress indicators in the UI:  
   `frontend/components/*.tsx`
3. Collapse technical details into expandable sections.
4. Test error scenarios and verify user experience.

---

### Add or Update Documentation and README
**Trigger:** When documenting new features, plans, or updating the README.  
**Command:** `/update-docs`

1. Create or update markdown documentation files:  
   - `ARCHITECTURE.md`
   - `OPTIMIZATION_PLAN.md`
   - `FUTURE_SCOPE_QA.md`
   - `README.md`
2. Update `.gitignore` or related config if needed.
3. Commit with a documentation-focused message.

---

## Testing Patterns

- **Framework:** Unknown (not detected)
- **File pattern:** `*.test.ts` for frontend tests
- **Python tests:** Not explicitly detected; consider using `pytest` or `unittest` for backend.
- **Example (TypeScript):**
  ```typescript
  // frontend/components/DocumentList.test.ts
  import { render } from '@testing-library/react';
  import DocumentList from './DocumentList';

  test('renders document list', () => {
    render(<DocumentList documents={[]} />);
    // assertions...
  });
  ```

## Commands

| Command           | Purpose                                                        |
|-------------------|----------------------------------------------------------------|
| /add-llm-provider | Add a new LLM provider integration to the pipeline             |
| /new-table        | Add or modify Supabase tables, columns, or policies            |
| /new-feature      | Implement a new feature spanning backend, frontend, and scripts|
| /new-gh-action    | Add or update GitHub Actions workflows and automation scripts  |
| /improve-errors   | Improve error handling and user feedback in the app            |
| /update-docs      | Add or update documentation files and README                   |
```
