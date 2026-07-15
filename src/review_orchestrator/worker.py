"""Compatibility alias for background worker services."""

from review_orchestrator._compat import alias_module

alias_module(__name__, "review_orchestrator.application.worker")
