"""Compatibility import; implementation lives in generator.editorial."""

import sys

from generator import editorial as _implementation

sys.modules[__name__] = _implementation
