# AI Agent Frontend

Godot 4 editor plugin frontend for the local `ai_agent_service`.

This folder is intentionally separate from the Godot engine source tree. To use it in a
Godot project, copy or symlink `ai_agent_frontend/addons/ai_agent` into that project's
`addons/` folder, then enable **AI Agent** from **Project > Project Settings > Plugins**.

The plugin talks to the Python service through local HTTP with a bearer token. Editor-only
settings are stored in `EditorSettings`, not `ProjectSettings`, so an untrusted project
cannot raise permissions by editing `project.godot`.
