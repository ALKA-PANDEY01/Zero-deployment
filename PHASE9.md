# Phase 9: GitHub Actions and ZeroDown

The `Test and deploy with ZeroDown` workflow runs on pushes to `main` and can
also be started with **Actions → Test and deploy with ZeroDown → Run workflow**.
Pull requests are validated by the separate CI workflow and do not deploy.

The workflow checks out the commit, installs Python dependencies, runs the
tests, validates Compose, builds the application image, and publishes it to
GHCR tagged with the full commit SHA. It then sends that immutable image
reference to ZeroDown and waits for the controller's deployment status. A
rollout succeeds only when ZeroDown reports `SUCCESS`; a rollback, failure,
or timeout fails the GitHub Actions job.

GitHub Actions only builds and publishes the image and requests a deployment.
ZeroDown pulls the image, validates the candidate, applies the configured
rollout stages, monitors safety limits, and decides whether to continue or
roll back.

## Use a self-hosted Windows runner

For a local demo, the workflow uses a **self-hosted GitHub Actions runner** on
the same PC as ZeroDown. This means the controller stays local and there is no
public address to create. The value of `ZERODOWN_URL` will be
`http://localhost:9000`.

1. Make sure Docker Desktop is running with Linux containers enabled. Install
   Git for Windows (which provides Bash) and Python 3.12 if they are not
   already installed.
2. In the GitHub repository, go to **Settings → Actions → Runners → New
   self-hosted runner**.
3. Select **Windows** and **x64**. Follow the setup commands GitHub displays
   to download and configure a runner in a folder such as `C:\actions-runner`.
   Keep the one-time runner registration token private; it is not the
   ZeroDown deployment token.
4. Open PowerShell in that runner folder and start `.\run.cmd`. Leave the
   window open while the workflow runs. This PC must remain awake, online, and
   running Docker Desktop.

The workflow is restricted to the `self-hosted`, `Windows`, and `X64` runner
labels. Do not run untrusted pull-request jobs on a self-hosted runner with
access to your local Docker daemon or deployment secrets.

## Configure GitHub secrets

Add these repository secrets under **Settings → Secrets and variables →
Actions**:

- `ZERODOWN_URL`: set this to `http://localhost:9000` for this local
  self-hosted runner (no trailing slash). The dashboard itself is at
  `http://localhost:9000/`.
- `ZERODOWN_DEPLOY_TOKEN`: a strong bearer token used to authorize the
  workflow's deployment request.

The workflow uses GitHub's automatically provided `GITHUB_TOKEN` to publish
the image and passes it to ZeroDown for the matching GHCR pull. The token is
not printed by the workflow or controller. HTTP is allowed only for a
localhost URL; remote controller URLs must use HTTPS.

Configure the same `ZERODOWN_DEPLOY_TOKEN` in the ignored `.env` file beside
`compose.yaml`:

```dotenv
ZERODOWN_DEPLOY_TOKEN=replace-with-a-long-random-token
```

Then recreate the controller so Compose passes the secret into its environment:

```bash
docker compose up -d --build --force-recreate controller
```

Do not commit `.env`, include the token in a URL, enable shell tracing, or log
the deployment request body. The CI endpoint accepts only immutable,
commit-SHA-tagged GHCR image references.

Do not create a public tunnel for this demo. The controller exposes operational
APIs in addition to the token-protected deployment trigger, so exposing port
9000 publicly without adding access controls would be unsafe.

## Workflow behavior

1. A push to `main` (or a manual run) executes tests and validates the Compose
   configuration.
2. On success, Docker builds `./app` and pushes
   `ghcr.io/<owner>/<repository>:<commit-sha>`.
3. The workflow calls `POST /api/v1/deploy` with the image reference, temporary
   GHCR pull credentials, and the bearer token.
4. ZeroDown runs its configured progressive rollout. The workflow polls
   `/deployment/status` for the matching deployment ID.
5. `SUCCESS` passes the workflow. `ROLLBACK`, `FAILED`, an unreachable
   controller, or a 15-minute timeout fails it and provides the controller's
   safe status summary.

The API trigger is separate from the dashboard's local `/deploy` endpoint.
Deployment decisions and safety thresholds remain in ZeroDown.
