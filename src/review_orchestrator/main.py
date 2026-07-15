"""Stable ASGI entry point for the presentation package."""

from review_orchestrator._compat import alias_module

alias_module(__name__, "review_orchestrator.presentation.main")
