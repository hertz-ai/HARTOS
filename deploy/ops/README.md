# Ops scripts (deployed to `/opt` on DeepBox)

These run on the DeepBox host, not inside a container, so they live here for
version control and are installed to `/opt`. They were written after finding
that the mail server had been serving a certificate that expired **2023-05-13
for over three years** — the renewal cron worked fine, it just had nowhere to
send the result.

## `ensure_ssh_access.sh`

Makes key-based SSH to maintenance targets reproducible. `deploy_mailserver()`
in the renewal script needs passwordless root SSH to the mail VM; that key was
first installed by hand, which does not survive rebuilding a box or adding
another DeepBox clone.

```bash
/opt/ensure_ssh_access.sh root@104.254.246.77          # re-pair (idempotent)
TARGET_PASS='...' /opt/ensure_ssh_access.sh root@new   # bootstrap a new host
HEVOLVE_SSH_TARGETS="root@a,root@b:2222" /opt/ensure_ssh_access.sh
```

Idempotent: a target that already accepts the key needs no password and is
left alone. A password is required only to bootstrap a host that does not yet
trust us. Exits 1 if any target still lacks key access, so cron can alarm.

## `renew_ssl_mailserver_deploy.py`

Reference copy of the `deploy_mailserver()` function added to
`/opt/renew_ssl_v2.py` (that script predates this repo and is not otherwise
version-controlled). It is registered in `_deploy_and_verify` alongside the
existing nginx / Kong / Tomcat / Crossbar targets, so the cert reaches the
mail VM on the same 6-hourly cron as everything else.

Context: `mail.hertzai.com` runs `docker-mailserver` on a separate IONOS VM,
and its `/etc/letsencrypt` lives **inside** the container with no bind mount —
which is why host renewals never reached it. `*.hertzai.com` covers
`mail.hertzai.com`, so the wildcard already being renewed is the right cert;
it only ever needed delivering.
