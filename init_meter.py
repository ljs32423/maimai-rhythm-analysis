#!/usr/bin/env python3
"""兼容入口；实际实现位于 mra.init_meter。"""
from mra.init_meter import main


if __name__ == "__main__":
    raise SystemExit(main())
