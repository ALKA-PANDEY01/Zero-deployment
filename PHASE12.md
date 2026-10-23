# Phase 12: Successful deployment demonstration

Start from the default V1 deployment:

```bash
docker compose up --build -d
```

From the dashboard at [http://localhost:9000](http://localhost:9000), select
**Deploy V2**. Alternatively, start a rollout from a terminal:

```bash
curl -X POST http://localhost:9000/deploy \
  -H "Content-Type: application/json" \
  -d '{"observation_seconds":1}'
```

ZeroDown builds and validates V2, shifts traffic through the configured
10%, 25%, 50%, and 100% stages, and checks health and safety metrics at each
stage. After the 100% observation passes, ZeroDown starts and validates a
rollback standby from the promoted image, switches the inactive V1 target to
that standby, and only then removes the old V1 container. V2 remains the
active version at 100% traffic.

The dashboard reports **DEPLOYMENT SUCCESSFUL**, marks V2 **ACTIVE** with 100%
traffic, and shows recent deployment history. The controller appends complete
deployment summaries and event logs to
`/data/deployment-history.jsonl` in the Compose setup; the mounted `data/`
directory keeps this history across controller restarts. The API exposes the
most recent 20 records at `/deployment/history`.
