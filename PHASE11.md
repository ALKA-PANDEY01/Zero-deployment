# Phase 11: Deterministic failure and rollback demonstration

The dashboard's **Run deterministic rollback demo** button, or the command
below, starts a real candidate deployment of the demo app:

```bash
curl -X POST http://localhost:9000/demo/failure \
  -H "Content-Type: application/json" \
  -d '{"observation_seconds":1}'
```

The controller starts V2 with the demo-only
`ZERODOWN_DEMO_FAILURE_AFTER_REQUESTS` setting. V2's health and readiness
checks continue to pass, but its normal `/` endpoint returns HTTP 500 after a
deterministic number of successful requests. The count accounts for the
configured stage duration and traffic shares so the error starts at the 50%
canary. The controller observes real gateway responses, applies the configured
error-rate limit, immediately restores 100% of traffic to V1, and removes the
failed candidate.

The dashboard displays the sequence **ROLLING OUT V2 → 10% → 25% → 50% →
FAILURE DETECTED → ROLLBACK → V1 RESTORED**, along with live metrics and
timestamped events.

For a repeatable local demonstration, run the default Compose stack first:

```bash
docker compose up --build -d
```

Then open [http://localhost:9000](http://localhost:9000) and click **Run
deterministic rollback demo**, or send the curl request above. The same request
can be repeated after the prior demo finishes; the failed V2 container is
removed and rebuilt for each run.

The demo requires configured rollout stages that include 10%, 25%, and 50%.
It uses a demo fault setting understood by this repository's sample app; it
does not add failure behavior to other applications or to the ZeroDown
controller's normal deployment path.
