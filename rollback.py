"""Trigger a rollback through the running deployment controller."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CONTROLLER_URL = os.getenv(
    "CONTROLLER_URL",
    "http://localhost:9000",
).rstrip("/")


def main():
    request = Request(
        f"{CONTROLLER_URL}/rollback",
        data=b"",
        headers={"Content-Length": "0"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"Controller rejected rollback ({error.code}): {detail}"
        ) from error
    except URLError as error:
        raise SystemExit(
            f"Could not reach deployment controller at {CONTROLLER_URL}: {error.reason}"
        ) from error

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
