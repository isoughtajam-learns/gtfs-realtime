# Deployment

## Three distinct ways this app runs — keep them isolated

1. **Local, no Docker** — running the server/fetcher/migrations directly on the host
   against a local Postgres. Configured via `.env.dev`.
2. **Local Docker** — `docker-compose.yml` at the repo root (db, redis, backend,
   celery-worker, celery-beat containers). Configured via `.env.dev_docker`.
3. **Production (AWS)** — Terraform in `deployment/`. Configured via `.env.prod` plus
   real environment variables injected at deploy time (see secrets, below).

A request to change one of these shouldn't bleed into the others — e.g. "update the
Terraform" should touch only `deployment/*.tf`, not `docker-compose.yml`. The Dockerfile
at the repo root is the one genuinely shared artifact (Terraform builds the same image
docker-compose does); a change there affects all three and should be called out as such.

**Gotcha**: on at least one development machine, a native (Homebrew) Postgres process
listens on the same port (`5432`) as docker-compose's `db` container's port mapping. The
more specific bind (native, `127.0.0.1`) wins over docker's wildcard bind, so a plain
`uv run alembic upgrade head` (or any local command reading `.env.dev`'s
`localhost:5432`) can silently operate on a *different* database than the actual
docker-compose stack. Verify which Postgres is actually being hit (e.g.
`lsof -nP -iTCP:5432 -sTCP:LISTEN`) before trusting a local DB operation if both are
ever running at once.

## Production infrastructure

Single EC2 instance (`t4g.small`, ARM64) running **ECS with the EC2 launch type** (not
Fargate — this was a deliberate switch from an earlier Fargate design, for cost/
simplicity, after starting with Fargate). Four services share the instance via `host`
network mode (not `bridge` — switched to let containers reach each other over
`localhost`/`127.0.0.1`):

- `backend`, `celery-worker`, `celery-beat` — one image, built from this repo's root
  `Dockerfile`, one ECR repo.
- `frontend` — a separate image from the sibling repo `gtfs-dashboard` (its own
  Dockerfile, own ECR repo), an nginx-served Vite/React SPA that reverse-proxies `/api/*`
  to the backend.

RDS Postgres and ElastiCache Redis remain managed services — container storage on EC2
launch type still isn't durable, same as Fargate. A Terraform-managed Elastic IP gives
the instance a stable address.

**`gtfs-realtime` and `gtfs-dashboard` are two independent Terraform stacks**, coupled
only by looking up shared resources by name (`var.app_name`) via Terraform `data`
sources — never by reading each other's state file. `gtfs-realtime`'s stack owns the EC2
instance, ECS cluster, RDS, ElastiCache, IAM roles, and the shared secrets-access IAM
policy; `gtfs-dashboard`'s stack only owns its own ECR repo/task definition/service and
looks up the cluster/execution-role/secrets it needs by name.

## Deploys only ever build from a tagged commit reachable from `origin/main`

Never the local working tree, never an unmerged branch. `deployment/tag-release.sh`
computes the next `vX.Y.Z` tag from `VERSION`, tags `origin/main`'s tip, and pushes
(interactive confirm — tags are shared state). The automated CI deploy (`.github/
workflows/deploy.yml`) computes its release tag as `v$(cat VERSION)`; **if that tag
already exists, it skips deploying** — this is the mechanism for merging a PR without
triggering a deploy (just don't bump `VERSION` in that PR).

A manual/fallback path, `deployment/deploy.sh`, exists for out-of-band hotfixes or
debugging the deploy itself: it builds via `git archive <tag> | docker build -` (a clean
export of that exact commit's tree, verified isolated from uncommitted changes) and runs
`terraform plan`/`apply` directly — notably, **it does not run database migrations at
all**, unlike the automated CI path. This matters for incident recovery (see
`incidents-and-lessons.md`'s migration-bootstrap-gap entry).

### Deliberately not bumping `VERSION`

A PR whose merge would make `terraform apply` or a migration fail outright (e.g.
referencing a not-yet-created secret, or genuinely destructive schema change) should
leave `VERSION` unchanged, so merging doesn't trigger a doomed/dangerous automated
deploy. The follow-up PR that bumps `VERSION` (once the external precondition is
actually satisfied) is a separate, deliberate step.

## Secrets

Real secrets (database URL, app secret key, TLS cert/key, third-party API keys) are
**never** written as literals in any committed file. Two patterns, both injected into
the ECS task definition as `secrets` (`valueFrom`, not plain `environment`):

- **Terraform-managed** (e.g. a `random_password`-generated DB password): a real
  `aws_secretsmanager_secret` + `_version` resource pair.
- **Externally-issued / hand-provisioned** (TLS cert/key, third-party API keys): a
  `data "aws_secretsmanager_secret"` lookup by name — Terraform doesn't generate these,
  a human creates them in Secrets Manager directly. Looked up by name rather than a
  hardcoded ARN because Secrets Manager appends a random suffix to the real ARN that a
  hand-typed literal won't include.

Either way, the IAM policy granting the shared ECS execution role read access
(`ecs_secrets_access`) needs the new secret's ARN added to its `Resource` list, or the
task will fail to start with an access-denied error, not a missing-secret error.

The application code (`src/settings.py`'s `Settings`, pydantic-settings `BaseSettings`)
reads real secrets as `Optional[str]` fields aliased to real environment variable names,
deliberately absent from every committed `.env.*` file. Locally, a developer exports
them in their own shell or an uncommitted `.env`; in production, Secrets Manager injects
them.

## Known gotchas (AWS/Terraform-specific)

- **Never change a security group's `description`** attached to RDS/ElastiCache — AWS
  treats it as immutable, forcing destroy-then-recreate, and destroying an SG still
  attached to a live ENI can hang for 15+ minutes before failing as a
  `DependencyViolation`.
- A security group referenced by another SG's ingress rule can hit the same destroy
  failure if Terraform doesn't sequence the revoke before the destroy — fix by manually
  revoking the stale rule via the AWS CLI rather than retrying the same apply.
- A single-instance ECS cluster with a fixed host port needs
  `deployment_minimum_healthy_percent = 0` / `deployment_maximum_percent = 100` — the
  default rolling-deployment behavior tries to start the new task before stopping the
  old one, which collides on the same host port and hangs forever.
- `docker login`/`docker build` hanging is usually a stuck Docker Desktop daemon (e.g.
  after the host machine slept), not a real network issue — diagnose with `docker info`/
  `docker version` (they'll hang too); fix by restarting Docker Desktop.
- SSH to the EC2 instance becoming unresponsive (banner-exchange timeout, not connection
  refused) reliably indicates real memory pressure on the box, even though EC2 status
  checks still report "ok" (they only check hypervisor/network reachability, not
  in-guest resource health). `aws ec2 reboot-instances` clears this without data loss.
- ECS EC2 launch type does not support changing an already-registered instance's EC2
  instance type in place — the ECS agent keeps a local checkpoint and will terminally
  exit on next boot if it detects a type mismatch, rather than re-registering. Fix
  without replacing the instance: force-deregister the container instance, then on the
  box, stop the ECS agent, delete its local state file, restart it.
- This AWS account is on a billing-restricted free plan that blocks non-free-tier EC2
  instance types at the API level — an account-level restriction, not something
  Terraform or IAM permissions can route around.
- `main.tf`'s AMI lookup floats to AWS's "latest recommended" AMI rather than a pinned
  value — *any* `terraform apply`, even for unrelated resources, can silently
  force-replace the EC2 instance if the AMI drifted since the last apply. This causes a
  new SSH host key (expected, verify via `describe-instances` rather than assuming
  something's wrong). Not pinned as of this writing.
