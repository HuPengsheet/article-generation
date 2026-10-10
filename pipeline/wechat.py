"""Compatibility import; implementation lives in shared.wechat."""

import sys

from shared import wechat as _implementation

sys.modules[__name__] = _implementation
