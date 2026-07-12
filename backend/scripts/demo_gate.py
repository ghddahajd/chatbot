"""Run the pre-demo regression pack through the real chat HTTP flow."""

from __future__ import annotations

import sys

from vendor_compare import main


if __name__ == "__main__":
    if "--demo-gate" not in sys.argv:
        sys.argv.append("--demo-gate")
    raise SystemExit(main())
