---
name: godot-code-reading
description: Read Godot project code safely before answering implementation questions.
when_to_use: Use when the user asks where behavior comes from, asks for code review, or needs file-backed analysis.
allowed-tools: [list_files, read_file, grep_code, read_class_docs]
paths: []
---

Before answering code questions, inspect the relevant project files with precise read-only tools.

Prefer this workflow:

1. Use `list_files` to locate likely files.
2. Use `grep_code` to find exact symbols, routes, tool names, or settings.
3. Use `read_file` for the final source of truth.
4. Use `read_class_docs` before writing code that calls Godot APIs.
5. Explain conclusions with concrete file paths and avoid guessing from names alone.
