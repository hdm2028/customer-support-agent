"""Emit aggregate alerts to stdout for a scheduler/monitoring collector.

Exit 0: no alerts; 1: active alerts; 2: observation unavailable. No messages
are sent, queued work is not consumed, and no payment is submitted.
"""
import argparse
import json
import sys

from app.core.config import get_settings
from app.observability.operations import operational_summary


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--window-seconds", type=int, default=900)
    args = parser.parse_args()
    get_settings()
    try:
        result = operational_summary(args.window_seconds)
    except Exception as error:
        print(json.dumps({"success": False, "error_type": type(error).__name__,
                          "alerts": [{"code": "observation_unavailable"}]}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["alerts"] else 0


if __name__ == "__main__":
    sys.exit(main())
