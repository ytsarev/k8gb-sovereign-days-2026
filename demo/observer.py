#!/usr/bin/env python3
"""One persistent request/DNS observer, outside both application clusters."""
import argparse
import concurrent.futures as futures
import json
import time

from core import load_config, query_dns, request_application


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = load_config(args.config)
    with futures.ThreadPoolExecutor(max_workers=4) as pool:
        while True:
            started = time.monotonic()
            jobs = {}
            for key in ("A", "B"):
                jobs["dns_" + key] = pool.submit(query_dns, cfg["hostname"],
                    cfg["clusters"][key]["dns_address"], 53, True)
            # This is an active query through the client's actual caching resolver,
            # not a passive cache dump or a predictor of the following HTTP lookup.
            jobs["resolver"] = pool.submit(query_dns, cfg["hostname"], "127.0.0.1")
            jobs["request"] = pool.submit(request_application, cfg["hostname"], cfg["clusters"])
            for name, job in jobs.items():
                print(json.dumps({"source": name, "data": job.result()}), flush=True)
            time.sleep(max(0, cfg["interval"] - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
