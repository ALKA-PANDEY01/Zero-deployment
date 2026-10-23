# Phase 10: Deploy another application with ZeroDown

ZeroDown treats `v1` and `v2` as stable/candidate release slots. The
application itself does not need to implement version routes or return a
particular JSON shape. It needs an HTTP health endpoint and, optionally, smoke
endpoints. ZeroDown checks the configured paths for HTTP 200 responses.

## Application template

```text
my-app/
├── Dockerfile
├── app/
│   └── ...
└── zerodown.yaml
```

Example configuration:

```yaml
app:
  name: my-app
  build_context: ./my-app
  dockerfile: Dockerfile
  port: 8000

health:
  endpoint: /health
  smoke_endpoints:
    - /

rollout:
  stages: [10, 25, 50, 100]

limits:
  max_error_rate: 5
  max_latency_ms: 1000
  max_cpu_percent: 90
  max_memory_percent: 90
```

`build_context` must be inside the project directory mounted into the
controller. Paths in this repository's Compose setup are relative to the
repository root. The example app and full configuration are in
[`examples/notes-service`](./examples/notes-service/).

## Run the second-application example locally

From the ZeroDown repository root:

```bash
docker compose -f compose.yaml -f examples/notes-service/compose.yaml up --build -d
```

This Compose override selects the Notes service's Dockerfile and configuration,
sets its internal port to `8081`, sets its health check to `/ready`, and points
the initial stable route at the Notes V1 container. The controller builds V2
from the same configured app and uses the same health and rollout settings.

Check the custom health endpoint and dashboard:

```bash
curl -i http://localhost:8080/ready
curl -sS http://localhost:9000/api/v1/config
```

Open `http://localhost:9000/` and select **Deploy V2**. The configured rollout
stages, application name, health checks, and metrics are shown live. This
sample returns plain text from `/` and `/ready`; it does not implement `/version`
or return a `version` field.

Return to the default demo application when done:

```bash
docker compose -f compose.yaml -f examples/notes-service/compose.yaml down
docker compose up --build -d
```

## Onboard another application

1. **Dockerize the application:** add a Dockerfile that starts the web service
   and listens on the configured port.
2. **Add a health endpoint:** provide an HTTP endpoint that returns status 200
   when the application is ready.
3. **Create `zerodown.yaml`:** set the application name, build context,
   Dockerfile, port, health/smoke paths, rollout stages, and safety limits.
4. **Connect the repository:** make the application files and configuration
   available under the controller's mounted project directory. For Compose,
   select the config with `ZERODOWN_CONFIG` and point the stable V1 service and
   proxy's initial route at the app using the approach in the Notes example.
5. **Push a new version:** publish the app image or let the configured build
   context be built by ZeroDown. GitHub Actions can publish an image and invoke
   the authenticated deployment API when Phase 9 is configured.
6. **Let ZeroDown deploy it:** the controller validates the candidate against
   configured endpoints, shifts traffic through the configured stages, checks
   safety limits, and rolls back to V1 if a check fails.

For a different app port or health path, update only the app's configuration,
Dockerfile/container setup, and initial stable-service/proxy wiring. The
ZeroDown controller's health checks and rollout logic do not need app-specific
code changes.
