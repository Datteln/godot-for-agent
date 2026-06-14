# AI Agent Frontend

Godot 4 editor plugin frontend for the local `ai_agent_service`.

This folder is intentionally separate from the Godot engine source tree. To use it in a
Godot project, copy or symlink `ai_agent_frontend/addons/ai_agent` into that project's
`addons/` folder, then enable **AI Agent** from **Project > Project Settings > Plugins**.

The plugin talks to the Python service through local HTTP with a bearer token. Editor-only
settings are stored in `EditorSettings`, not `ProjectSettings`, so an untrusted project
cannot raise permissions by editing `project.godot`.

Configure the local LLM base URL, API key, model, fallback model, and request timeout
from **Editor > Editor Settings > AI Agent**. These values are applied to auto-started
services through `AI_AGENT_LLM_*` environment variables; manually started services must
be restarted with matching environment variables.
Set `llm_quick_model`, `llm_standard_model`, `llm_deep_model`, `llm_verify_model`, and
`llm_advisor_model` to route different effort levels to cheaper or stronger models.

Frontend logs are controlled from **Editor > Editor Settings > AI Agent** with
`log_level`, `log_to_file`, and `log_file_path`. Logs always go to the Godot Output
panel at the configured level, and can optionally be appended to a file.

The chat dock renders basic Markdown, supports selecting/copying output text, and keeps
Doctor, Memory, Extensions, Commands, and tool-call summaries inside the conversation
instead of opening extra result windows.
