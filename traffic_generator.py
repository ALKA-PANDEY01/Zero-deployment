"""Generate repeatable HTTP traffic for the canary deployment demo."""

import argparse
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def make_request(url: str, timeout: float):
    try:
        with urlopen(url, timeout=timeout) as response:
            status = response.status
            payload = response.read()
        version = json.loads(payload.decode("utf-8")).get("version")
        return status, version, None
    except HTTPError as error:
        return error.code, None, f"HTTP {error.code}"
    except (URLError, TimeoutError, ValueError) as error:
        return None, None, str(error)


def main():
    parser = argparse.ArgumentParser(
        description="Send traffic through Nginx and summarize V1/V2 responses."
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8080/",
        help="Nginx URL (default: %(default)s)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30,
        help="Run duration in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--rps",
        type=float,
        default=10,
        help="Request rate per second (default: %(default)s)",
    )
    args = parser.parse_args()

    if not 0 < args.duration <= 600:
        parser.error("--duration must be greater than 0 and at most 600")
    if not 0 < args.rps <= 200:
        parser.error("--rps must be greater than 0 and at most 200")

    interval = 1 / args.rps
    max_workers = max(1, min(100, round(args.rps)))
    futures = []
    started = time.monotonic()
    next_request = started

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while time.monotonic() - started < args.duration:
            now = time.monotonic()
            if now >= next_request:
                futures.append(
                    executor.submit(make_request, args.url, 5)
                )
                next_request += interval
            else:
                time.sleep(min(next_request - now, 0.01))

    statuses = Counter()
    versions = Counter()
    errors = []
    for future in as_completed(futures):
        status, version, error = future.result()
        if status is not None:
            statuses[status] += 1
        if version:
            versions[version] += 1
        if error:
            errors.append(error)

    print(f"URL: {args.url}")
    print(f"Requests: {len(futures)} over {args.duration:g}s")
    print(f"HTTP statuses: {dict(sorted(statuses.items()))}")
    print(f"Responses by version: {dict(sorted(versions.items()))}")
    print(f"Request errors: {len(errors)}")
    for error in errors[:5]:
        print(f"  {error}")


if __name__ == "__main__":
    main()
