"""Allow ``python -m cost_sentinel`` as an alias for the ``cost-sentinel`` script."""

from __future__ import annotations

import sys

from cost_sentinel.cli import main

if __name__ == "__main__":
    sys.exit(main())
