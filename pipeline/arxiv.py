"""Compatibility import; implementation lives in arxiv_tool.collector."""

import sys

from arxiv_tool import collector as _implementation

sys.modules[__name__] = _implementation
