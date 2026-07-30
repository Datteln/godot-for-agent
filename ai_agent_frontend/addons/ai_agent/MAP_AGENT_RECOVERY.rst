Map Agent transaction recovery
==============================

Map writes are blocked until the editor reconciles
``res://.ai_agent_service/map_agent/transactions``. A journal is durable in
one of these states::

  prepared -> applying -> committing -> committed
  prepared/applying -> rolled_back

``cleaned`` is not persisted. It means the matching terminal journal and
snapshot files are absent.

Durability boundary inventory
-----------------------------

Godot write transactions use these named boundaries, in order:

1. transaction directory creation;
2. scene snapshot write and flush;
3. ``prepared`` journal write and flush;
4. each operation application plus ``applying`` journal write and flush;
5. authoritative revision-file write and flush;
6. ``committing`` journal write and flush;
7. Undo action commit;
8. ``committed`` terminal write and flush, or before-state restoration followed
   by ``rolled_back`` terminal write and flush;
9. snapshot/non-terminal cleanup;
10. terminal-journal cleanup.

Python tool-result publication uses these named boundaries:

1. clone active Session into a transaction-local working Session;
2. stage artifact entries and compute canonical entry/turn fingerprints;
3. write the versioned coordinated record and prepared artifact document;
4. publish the Session document containing exact locators;
5. mark the artifact turn and coordinated record committed;
6. remove the coordinated record;
7. publish buffered business events and resolve previews.

Process exit is a relevant boundary before and after every durable write,
rename, resource publication, terminal marker, and cleanup step. Heartbeats
are deliberately outside both state machines and never mutate business state.

Automatic recovery
------------------

* ``prepared`` and ``applying`` restore their recorded before-state. A
  checksummed ``rolled_back`` marker is written before cleanup.
* ``committed`` and ``rolled_back`` are terminal. Cleanup is retried and the
  terminal marker is removed last.
* ``committing`` is ambiguous and fails closed. The editor does not guess
  whether the Undo action committed.
* Unknown schema versions or states, checksum failures, missing or mismatched
  snapshots, oversized journals, and excessive operation counts fail closed.

Recovery status exposes phase, transaction id, durable status, elapsed time,
and configured bounds. The current journal, snapshot, operation, tool,
duration, and latency policies are defined by ``map_transaction_policy.gd``.

Before every map mutation, recovery completes first. The revision tracker then
reloads authoritative revision metadata, synchronizes content fingerprints,
and performs the expected-revision compare-and-swap before journal creation,
Undo action creation, or content mutation. Keyboard and programmatic Undo/Redo
use the same synchronization path.

Typed failure codes
-------------------

* ``map_transaction_recovery_in_progress``
* ``map_transaction_recovery_required``
* ``map_transaction_journal_corrupt``
* ``map_transaction_journal_checksum_mismatch``
* ``map_transaction_journal_schema_unsupported``
* ``map_transaction_journal_state_invalid``
* ``map_transaction_journal_oversized``
* ``map_transaction_recovery_operation_limit``
* ``map_transaction_commit_outcome_ambiguous``
* ``map_transaction_committed_marker_write_failed``
* ``map_transaction_rolled_back_marker_write_failed``
* ``map_transaction_cleanup_failed``
* ``map_revision_conflict``
* ``platform_approval_identity_invalid``
* ``platform_approval_identity_conflict``

Manual ambiguous-state recovery
-------------------------------

1. Stop automatic map writes and copy the entire transaction directory.
2. Inspect the latest checksummed ``committing`` journal, its target,
   base/latest revisions, approval records, before/after fingerprints, and
   scene snapshot.
3. Compare the authoritative revision file and current map content with both
   sides of the journal. Do not delete the journal merely because the scene
   opens successfully.
4. If the after-state and committed Undo history are proven, write or retain a
   valid ``committed`` terminal marker. If the before-state is restored and
   proven, write or retain ``rolled_back``.
5. Restart the editor and let terminal cleanup run. If cleanup still fails,
   correct filesystem permissions and retry; the terminal marker must remain.

For rollback, restore the scene snapshot and recorded file/tile before-values
in reverse operation order, restore authoritative revision metadata, then
persist ``rolled_back``. Never roll back a ``committing`` journal without
first proving that commit did not occur.
