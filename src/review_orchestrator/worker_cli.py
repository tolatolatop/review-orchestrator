"""Compatibility alias for the worker CLI entry point."""

from review_orchestrator._compat import alias_module

alias_module(__name__, "review_orchestrator.application.worker_cli")
