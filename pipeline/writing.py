"""Compatibility import; implementation lives in generator.writer."""

import sys

from generator import writer as _implementation

sys.modules[__name__] = _implementation
