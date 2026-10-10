"""Compatibility import; implementation lives in generator.legacy."""

import sys

from generator import legacy as _implementation

sys.modules[__name__] = _implementation
