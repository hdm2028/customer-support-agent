"""Consume refund events with graceful shutdown; does not submit payments."""
import argparse
import json
import math
import signal
from threading import Event
from time import perf_counter

from app.services.refund_service import process_refund_tasks


def run_worker(*, once=False, interval=2, batch_size=10, max_backoff=None, stop=None):
    if not math.isfinite(interval) or interval <= 0 or not 1 <= batch_size <= 100:
        raise ValueError("Invalid interval or batch size")
    max_backoff = max(30, interval) if max_backoff is None else max_backoff
    if not math.isfinite(max_backoff) or max_backoff < interval:
        raise ValueError("max_backoff must be finite and at least interval")
    stop = stop or Event()
    backoff = interval
    while not stop.is_set():
        started = perf_counter()
        try:
            result = process_refund_tasks(limit=batch_size)
            failures = sum(not row.get("success", False) for row in result["results"])
            report = {"event": "refund_worker_batch", "processed": result["processed"], "failures": failures,
                      "business_executed": sum(bool(r.get("business_executed")) for r in result["results"])}
            code = 1 if failures else 0
            # Per-message retries already have durable queue delays and limits.
            delay = interval
            backoff = interval
        except Exception as error:
            report = {"event": "refund_worker_unavailable", "error_type": type(error).__name__}
            code = 2
            delay = backoff
            backoff = min(max_backoff, backoff * 2)
        report["duration_ms"] = round((perf_counter() - started) * 1000, 2)
        report["next_poll_seconds"] = 0 if once or stop.is_set() else delay
        print(json.dumps(report), flush=True)
        if once:
            return code
        stop.wait(delay)
    return 0


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--max-backoff", type=float, default=None,
                        help="Maximum delay after batch errors (default: max(30, interval))")
    args = parser.parse_args()
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return run_worker(once=args.once, interval=args.interval, batch_size=args.batch_size,
                      max_backoff=args.max_backoff, stop=stop)


if __name__ == "__main__":
    raise SystemExit(main())
