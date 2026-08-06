"""Shared pytest fixtures for MUSAEUS safety regressions."""

# Importing registers the fixture without making safety guards global: only tests
# that request ``disposable_vault`` receive its temporary environment and guards.
from tests.disposable_vault import disposable_vault

__all__ = ["disposable_vault"]
