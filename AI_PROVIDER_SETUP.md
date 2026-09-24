# AI provider selection

The dashboard exposes the provider selector to administrators. The selected provider is stored in the backend PostgreSQL `config` row accessed through the existing `POSTGRES_URL`. On startup, `init_db()` adds the `ai_provider` column if it is missing, defaulting to Claude. No separate Supabase credentials or migration are needed.

Set `OPENAI_API_KEY` in the backend environment to use OpenAI. `OPENAI_MODEL` is optional and defaults to `gpt-4o-mini`. OpenAI requests use the already-installed `httpx` dependency. The existing Claude API keys and per-workflow rubric key behavior remain in use when Claude is selected.
