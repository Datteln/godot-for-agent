## Selected limits

- Backend event page default: 50 events.
- Backend event page hard maximum: 200 events.
- Chat-panel render budget: 24 events or 6 ms per editor frame, whichever is reached first.
- EventStore retention remains 500 events per Session.

These values bound both the transport response and the UI layout work without changing the configured one-second idle polling cadence. While `has_more=true`, the client repolls immediately after the current event request disconnects.

## Burst comparison

The captured pre-change timeline published 203 buffered events together at 19:20:47, followed by another 41-event response at 19:21:22. The first response could therefore synchronously trigger 203 event handlers and their associated layout work in one editor idle cycle.

With the selected limits, a 203-event backlog is returned as 50/50/50/50/3 events. The client advances its cursor only through accepted events and immediately requests the next page while backlog remains. The chat panel handles at most 24 events in one frame, so the same synthetic 203-event burst spans at least nine render batches while retaining sequence order and every append-only delta.

During atomic tool-result submissions, text and reasoning chunks now enter EventStore as provisional previews as soon as the provider callback fires. Transactional tool, grant, workflow, artifact, and final events remain buffered. A successful Session save and publication emits `submission_preview_committed`; cancellation, reducer failure, or persistence failure emits `submission_preview_discarded` without publishing staged facts.

## Verification results

- Backend streaming/paging/lifecycle suite: 13 passed.
- Full backend suite: 189 passed, 9 failed. The nine failures are the pre-existing stale map-agent/default-setting assertions recorded before this change; no new failure is in event streaming or atomic submission code.
- Godot headless event-client, 203-event backlog, follow-mode, and existing stream tests completed without test assertion failures.
- Godot emitted host-environment root-certificate and object-leak warnings during headless shutdown; product scripts passed `--check-only`.

The live long map-agent continuation reproduction still requires a configured provider and interactive editor Session. Automated coverage reproduces the transaction boundary with a paused streaming provider and reproduces the 203-event UI burst deterministically.
