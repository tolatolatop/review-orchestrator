"""Compatibility helpers for legacy module paths.

The implementation lives in responsibility-oriented packages.  Legacy module
paths remain aliases so existing integrations observe the same module object,
including runtime monkeypatches.
"""

import sys
from importlib import import_module
from types import ModuleType


def alias_module(legacy_name: str, target_name: str) -> ModuleType:
    target = import_module(target_name)
    sys.modules[legacy_name] = target
    parent_name, _, attribute = legacy_name.rpartition(".")
    if parent_name:
        setattr(sys.modules[parent_name], attribute, target)
    return target
