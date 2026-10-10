"""Compatibility import; implementation lives in shared.llm."""

import sys

from shared import llm as _implementation

sys.modules[__name__] = _implementation
