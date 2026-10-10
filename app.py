"""Compatibility entry point for the optional web workbench."""

import sys

from web import app as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
