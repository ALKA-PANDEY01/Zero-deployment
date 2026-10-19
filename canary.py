"""Run and follow a canary deployment through the Compose controller."""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_json(url: str, method: str = "GET", payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Controller returned HTTP {error.code}: {detail}"
        ) from error
    except URLError as error:
        raise RuntimeError(
            f"Could not reach the controller at {url}: {error.reason}"
        ) from error


def parse_timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def run():
    parser = argparse.ArgumentParser(
        description=(
            "Start the project's automated canary rollout and follow its status."
        )
    )
    parser.add_argument(
        "--controller-url",
        default="http://localhost:9000",
        help="Controller base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--observation-seconds",
        type=int,
        default=15,
        help="Final 100%% observation window (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1,
        help="Seconds between status checks (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="Maximum time to wait for completion (default: %(default)s)",
    )
    parser.add_argument(
        "--demo-failure",
        action="store_true",
        help="Intentionally fail V2 during observation to demonstrate rollback.",
    )
    args = parser.parse_args()

    if not 1 <= args.observation_seconds <= 300:
        parser.error("--observation-seconds must be between 1 and 300")
    if args.demo_failure and args.observation_seconds < 3:
        parser.error("--demo-failure requires at least 3 observation seconds")
    if args.poll_interval <= 0 or args.timeout <= 0:
        parser.error("--poll-interval and --timeout must be positive")

    controller_url = args.controller_url.rstrip("/")
    deploy_path = "/demo/failure" if args.demo_failure else "/deploy"
    start_time = datetime.now(timezone.utc)

    payload = {"observation_seconds": args.observation_seconds}
    accepted = request_json(
        f"{controller_url}{deploy_path}",
        method="POST",
        payload=payload,
    )
    print(accepted.get("message", "Deployment accepted"), flush=True)

    deadline = time.monotonic() + args.timeout
    printed_steps = 0
    deployment_started = False
    terminal_states = {"succeeded", "rolled_back", "failed"}

    while time.monotonic() < deadline:
        status = request_json(f"{controller_url}/status")
        started_at = parse_timestamp(status.get("started_at"))

        if started_at and started_at >= start_time:
            deployment_started = True
            for step in status.get("steps", [])[printed_steps:]:
                print(f"[{step.get('time', '')}] {step.get('message', '')}", flush=True)
            printed_steps = len(status.get("steps", []))

            if (
                status.get("finished_at")
                and status.get("status") in terminal_states
            ):
                final_status = status.get("status")
                print(f"Deployment finished: {final_status}")
                if args.demo_failure and final_status == "rolled_back":
                    print("Failure demo passed: traffic returned to V1.")
                successful = final_status == "succeeded" or (
                    args.demo_failure and final_status == "rolled_back"
                )
                return 0 if successful else 1

        time.sleep(args.poll_interval)

    if not deployment_started:
        raise RuntimeError("The controller did not start the deployment")
    raise RuntimeError("Timed out while waiting for the deployment to finish")


if __name__ == "__main__":
    try:
        sys.exit(run())
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
