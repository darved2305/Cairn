# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository — the
**Security** tab, then **Report a vulnerability**. Please do not open a public
issue for anything that would give an attacker a working exploit before a fix
exists.

Include what you ran, what you observed, and what you expected. A reproduction
against a local cluster (`./scripts/local_cluster.sh up`) is more useful than a
description.

## Credential handling in this repository

Cairn reads every credential from the process environment or from AWS Secrets
Manager. Repository configuration is designed to keep runtime credentials
outside source control:

- `.env` is gitignored; `.env.example` carries placeholders only and is the
  single committed env file.
- `scripts/provision_cluster.sh` writes the live connection string to `.env`,
  never to a tracked file.
- Terraform state, plan files, and `*.tfvars` are gitignored. `terraform plan
  -out=` files are treated as secrets-adjacent, because a saved plan can embed
  the value of a variable marked `sensitive = true`.
- `.gitignore` covers `*.pem`, `*.key`, `*.p12`, `*.pfx`, and `credentials*`.
- The deployed console reads its database URL from its own Secrets Manager
  secret. `db/migrations/0008_console_readonly_role.sql` creates a `SELECT`-only
  role for it; `scripts/provision_console_role.py` creates the login user and
  then *proves* the result by reconnecting as that user and asserting a write
  is rejected.
- CI holds no static AWS keys. `.github/workflows/ci.yml` specifies GitHub OIDC
  role assumption — not `AWS_SECRET_ACCESS_KEY` — for the optional Bedrock
  paths, and its integration job refuses to run at all without a real cluster
  credential rather than silently degrading to a mock.

`certs/local-ca.crt` is committed as an empty file on purpose. It exists so
developers behind a TLS-intercepting proxy can trust their own root during
`docker build` **locally**, via `git update-index --skip-worktree`. See
[`certs/README.md`](certs/README.md). Never commit a real certificate there.

## What Cairn executes

`cairn scout` and `cairn exec` supervise a command you give them. Cairn does not
sandbox that command: it observes it. Under `--contract shadow` nothing is ever
reused, and opaque arbitrary execution is frozen at `SHADOW_UNQUALIFIED`
coverage, so an observation alone can never authorize reuse. Run Cairn against
commands you already trust to run on that machine.

`--network deny` is a declared property of the execution contract that the
tracer *checks*, not a network namespace that blocks traffic: socket activity
without a stable adapter downgrades coverage to `INCOMPLETE_NETWORK`, which
makes the result non-reusable. It fails closed; it does not sandbox. See
[`docs/security/SECURITY_MODEL.md`](docs/security/SECURITY_MODEL.md) for the
full boundary, and the *Support boundary* section of the README for where the
reuse guarantees stop.

## Supported versions

Cairn is at `0.1.0` and pre-release. Fixes land on `main`; there is no
backport branch.
