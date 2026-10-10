"""Compatibility import; implementation lives in generator.figures.html_img."""

import sys

from generator.figures import html_img as _implementation

sys.modules[__name__] = _implementation
