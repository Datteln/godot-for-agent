# Clean-Cut Schema Boundary

The accepted runtime uses Session schema version `10` and schema epoch
`workflow-manifest-v1`. A Session is current only when both identifiers match and
its Map workflow is selected by the current manifest format.

Any Session lacking either identifier, carrying an earlier schema version, or
embedding the removed bounded `MapTaskState.workflow_events` representation is a
legacy Session. The runtime returns `unsupported_session_schema`, performs no
migration, provider request, tool execution, workflow write, or compatibility
read, and offers only creation of a new Session. The legacy files remain
untouched as user-managed backup.
