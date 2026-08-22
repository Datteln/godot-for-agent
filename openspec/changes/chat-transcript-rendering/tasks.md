## 1. Typed renderer boundary

- [ ] 1.1 Confirm the authoritative transcript entry kinds and typed render payload fields consumed by each renderer, including Thought content, token count, state, and completed duration.
- [ ] 1.2 Define a renderer registry, read-only render context, canonical copy-text adapter, and mounted-control update/reset contract.
- [ ] 1.3 Add fixtures for equal-text entries, Thought streaming/completion/expansion/copy after reload, thinking-budget boundaries, terminal-state regression rejection, revision updates, malformed Markdown, resolved approvals, compact tool results, and partial failures.

## 2. Text and status renderers

- [ ] 2.1 Adapt user and assistant transcript entries to safe Markdown renderers with readable fallback and canonical copy actions.
- [ ] 2.2 Implement per-entry revision updates that preserve compatible selection, focus, and expansion state without appending a second root control.
- [ ] 2.3 Adapt task progress, verification, and generic status entries to typed renderers without parsing display text.
- [ ] 2.4 Implement a Thought renderer that displays `Thinking {token_count} Tokens >` while thinking and `Thought for {duration_seconds}s >` once the original model stream completes; expand and copy only the persisted canonical Thought content.

## 3. Tool, approval, and error interaction

- [ ] 3.1 Adapt tool activity/result entries to compact stateful cards with expanded structured details only on request.
- [ ] 3.2 Adapt approval entries so only actionable state exposes accept/reject controls and every resolved state is read-only after reload or remount.
- [ ] 3.3 Adapt error entries to show operation context, known modification status, and retry only when the typed payload permits it.

## 4. Panel migration and verification

- [ ] 4.1 Replace ChatPanel direct role/text append and text-fingerprint display paths with the transcript rendering host after the authoritative Store is available.
- [ ] 4.2 Remove all text-inferred Thought rendering and ensure renderer inputs can create a Thought card only from typed `kind=thought` entries.
- [ ] 4.3 Exercise live, history, reconnect, remount, Thought expansion and collapsed/expanded copy, Markdown, tool, approval, verification, and error renderer fixtures.
- [ ] 4.4 Headless-load the plugin and manually verify Thought summaries and expansion after reload, plus copy, Markdown, approval resolution, compact tool details, and error context in a real session.
