"""Compatibility import; implementation lives in generator.figures.latex."""

import sys

from generator.figures import latex as _implementation

sys.modules[__name__] = _implementation
