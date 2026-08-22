## 1. Typed renderer boundary

- [x] 1.1 Confirm the authoritative transcript entry kinds and typed render payload fields consumed by each renderer, including Thought content, token count, state, completed duration, resolved-approval operation summary/path/outcome fields, and independently ordered supplemental `kind=user` entries.
- [x] 1.2 Define a renderer registry, read-only render context, canonical copy-text adapter, and viewport-owned mounted-control create/update/reset contract.
- [x] 1.3 Add fixtures for equal-text entries, oversized content preview/display-complete/evict/remount with full-content copy for every long-content kind, Thought streaming/completion/expansion/copy after reload, thinking-budget boundaries, terminal-state regression rejection, revision updates, malformed Markdown, one-line resolved-approval text nodes plus separate supplemental user entries, compact tool results, transient-notice discard, and partial failures.

## 2. Text and status renderers

- [x] 2.1 Adapt user and assistant transcript entries to safe Markdown renderers with readable fallback, canonical copy actions, configurable initial preview budget, explicit display-complete action, and release-on-eviction behavior.
- [x] 2.2 Implement per-entry revision updates that preserve compatible selection, focus, and expansion state without appending a second root control.
- [x] 2.3 Adapt task progress, verification, and generic status entries to typed renderers without parsing display text.
- [x] 2.4 Implement a Thought renderer that displays `Thinking {token_count} Tokens >` while thinking and `Thought for {duration_seconds}s >` once the original model stream completes; preview oversized expanded content, render it fully only after explicit user action, and copy the complete persisted canonical Thought content.

## 3. Tool, approval, and error interaction

- [x] 3.1 Adapt tool activity/result entries to compact stateful cards with expanded structured details only on request.
- [x] 3.2 Persist resolved-approval operation summary, affected paths, and outcome in the typed payload; adapt approval entries so only actionable state exposes accept/reject controls and every accepted/rejected/submitted state replaces the card with its one-line permission-result text after live resolution, reload, or remount. Persist any user supplemental input as a separate ordered `kind=user` entry.
- [x] 3.3 Adapt error entries to show operation context, known modification status, and retry only when the typed payload permits it.
- [x] 3.4 Move waiting/command-running local notices to a discardable transient host outside transcript Store and Viewport ownership.

## 4. Panel migration and verification

- [x] 4.1 Replace ChatPanel direct role/text append and text-fingerprint display paths with the transcript rendering host after the authoritative Store is available.
- [x] 4.2 Remove all text-inferred Thought rendering and ensure renderer inputs can create a Thought card only from typed `kind=thought` entries.
- [x] 4.3 Exercise live, history, reconnect, remount, Thought expansion and collapsed/expanded copy, Markdown, tool, approval, verification, and error renderer fixtures.
- [x] 4.4 Headless-load the plugin and manually verify Thought summaries and expansion after reload, plus copy, Markdown, approval resolution, compact tool details, and error context in a real session.

## 5. Post-release revisions

- [x] 5.1 Unchanged-content completion revisions must not rebuild the rich text (no flicker); full rebuilds happen only on history mount, display-mode switch, content replacement, or the completion self-heal comparison; the streaming cursor is independent of the rich text.
- [x] 5.2 Fix the Thought expansion arrow pivot (rotate around its center) so expanding no longer shifts the glyph position.
- [x] 5.3 Wrap the transcript list and the transient notice list in a single ScrollContainer child so the confirmation host and transient notices actually scroll into view.
- [x] 5.5 Render transient system notices at their triggering chat position instead of appending every notice to a fixed container after the durable transcript list; cover empty-history and waiting-after-optimistic-message fixtures, and verify discarded notices never enter Store/history.
- [x] 5.6 Bound synchronously rendered historical tool summaries (especially grep matches) so large persisted logs cannot freeze the Godot UI; preserve raw data in Store and add a large-match regression fixture.
