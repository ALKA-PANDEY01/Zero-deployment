"""Check availability and response versions during the 10% canary stage."""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_json(url, method="GET", payload=None, timeout=5):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def probe(url, timeout):
    try:
        with urlopen(url, timeout=timeout) as response:
            status = response.status
            body = response.read()
        version = json.loads(body.decode("utf-8")).get("version")
        return status, version, None
    except HTTPError as error:
        return error.code, None, f"HTTP {error.code}"
    except (URLError, TimeoutError, ValueError) as error:
        return None, None, str(error)


def wait_for_canary(controller_url, previous_started_at, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = request_json(f"{controller_url}/status")
        if status.get("started_at") != previous_started_at:
            messages = [
                step.get("message", "")
                for step in status.get("steps", [])
            ]
            if "Canary stage started: V2=10%" in messages:
                return
            if status.get("finished_at"):
                raise RuntimeError(
                    "Deployment finished before the 10% canary stage. "
                    "Check V2 health and controller logs."
                )
        time.sleep(0.05)

    raise RuntimeError("Timed out waiting for the 10% canary stage")


def main():
    parser = argparse.ArgumentParser(
        description="Check proxy availability during the 10% V2 canary."
    )
    parser.add_argument(
        "--controller-url",
        default="http://localhost:9000",
    )
    parser.add_argument(
        "--proxy-url",
        default="http://localhost:8080",
    )
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()

    if args.requests < 1 or args.concurrency < 1 or args.timeout <= 0:
        parser.error("requests, concurrency, and timeout must be positive")

    controller_url = args.controller_url.rstrip("/")
    proxy_url = args.proxy_url.rstrip("/")
    print("Starting deployment availability test...", flush=True)

    before = request_json(f"{controller_url}/status")
    if before.get("status") in {"validating", "rolling_back"}:
        raise RuntimeError("A deployment is already running")
    previous_started_at = before.get("started_at")

    # Put the baseline on V1, then confirm V1 is serving before rollout.
    request_json(f"{controller_url}/rollback", method="POST", payload={})
    baseline_deadline = time.monotonic() + 5
    while True:
        baseline = request_json(f"{proxy_url}/health")
        if baseline.get("version") == "v1":
            break
        if time.monotonic() >= baseline_deadline:
            raise RuntimeError(
                f"Expected V1 before deployment, got {baseline.get('version')}"
            )
        time.sleep(0.05)

    request_json(
        f"{controller_url}/deploy",
        method="POST",
        payload={"observation_seconds": 1},
    )
    wait_for_canary(
        controller_url,
        previous_started_at,
        args.timeout,
    )

    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(probe, f"{proxy_url}/", 5)
            for _ in range(args.requests)
        ]
        for future in as_completed(futures):
            results.append(future.result())
    duration = time.monotonic() - started

    successful = sum(status == 200 for status, _, _ in results)
    failed = args.requests - successful
    versions = {
        "v1": sum(version == "v1" for _, version, _ in results),
        "v2": sum(version == "v2" for _, version, _ in results),
    }

    print(f"Total requests: {args.requests}")
    print(f"Successful HTTP 200 responses: {successful}")
    print(f"Failed requests: {failed}")
    print(f"Duration: {duration:.2f} seconds")
    print(f"Responses by version: {versions}")

    if failed:
        for _, _, error in results:
            if error:
                print(f"Request error: {error}", file=sys.stderr)
        print("FAIL: Requests failed during this test.")
        return 1

    print("PASS: No failed requests during this test.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, HTTPError, URLError, TimeoutError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
