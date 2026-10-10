"""Compatibility import; implementation lives in web.store."""

import sys

from web import store as _implementation

sys.modules[__name__] = _implementation
