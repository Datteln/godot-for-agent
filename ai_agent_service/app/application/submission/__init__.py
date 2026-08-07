"""Concrete submission use cases.

Each use case owns its command validation, Session unit-of-work,
idempotency, recovery disposition, and response boundary.  They do NOT
hold an ``AgentApplication`` reference — they receive cohesive services
through explicit constructor injection.
"""
