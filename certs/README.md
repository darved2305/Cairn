# Local build certs

`local-ca.crt` is an intentionally empty placeholder. The Dockerfile
trusts it as an extra CA during `docker build`, which is a no-op with an
empty file.

If your machine's antivirus or corporate proxy does TLS interception
(re-signs outbound HTTPS with its own root cert — Avast, Kaspersky, many
corporate MITM proxies do this), `docker build` will fail with
`CERTIFICATE_VERIFY_FAILED` trying to reach PyPI. Fix it locally, without
touching the Dockerfile or committing anything security-sensitive:

```sh
# Tell git to stop tracking local edits to this file BEFORE changing it,
# so your real cert never ends up in a commit or diff:
git update-index --skip-worktree certs/local-ca.crt

# Example for Avast on Windows:
cp "/c/ProgramData/Avast Software/Avast/wscert.pem" certs/local-ca.crt
```

Never commit a real certificate here — `--skip-worktree` above is what
keeps that from happening by accident.
