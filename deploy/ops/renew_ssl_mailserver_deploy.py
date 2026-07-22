"""Reference copy of deploy_mailserver(), as deployed in /opt/renew_ssl_v2.py
on DeepBox. Registered in _deploy_and_verify() so the wildcard cert is pushed
to the mail VM on every renewal. See README.md in this directory.

This is a copy for version control -- the running file is /opt/renew_ssl_v2.py,
which predates this repo.
"""

# ── remote mail server (docker-mailserver on the IONOS VM) ───────────
MAIL_HOST      = os.environ.get("HEVOLVE_MAIL_HOST", "104.254.246.77")
MAIL_SSH_USER  = os.environ.get("HEVOLVE_MAIL_SSH_USER", "root")
MAIL_CONTAINER = os.environ.get("HEVOLVE_MAIL_CONTAINER", "mailserver")
MAIL_CERT_DIR  = os.environ.get("HEVOLVE_MAIL_CERT_DIR",
                                "/etc/letsencrypt/live/mail.hertzai.com")
MAIL_KEY       = os.environ.get("HEVOLVE_MAIL_SSH_KEY", "/root/.ssh/id_ed25519")


def deploy_mailserver(cert_dir):
    """Push the wildcard cert to the remote docker-mailserver VM.

    mail.hertzai.com runs docker-mailserver on a separate IONOS VM, and its
    /etc/letsencrypt lives INSIDE the container with no bind mount, so the
    renewals done here never reached it. It served a cert that expired
    2023-05-13 for over three years, which broke verified TLS on 465/587.

    *.hertzai.com covers mail.hertzai.com, so the cert this script already
    renews is the correct one; it only ever needed delivering. Without this
    step the remote goes stale again at the next renewal.
    """
    ssh = ["ssh", "-i", MAIL_KEY, "-o", "BatchMode=yes",
           "-o", "StrictHostKeyChecking=no",
           "%s@%s" % (MAIL_SSH_USER, MAIL_HOST)]
    stage = "/root/newcert"
    run(ssh + ["mkdir -p " + stage], check=False, timeout=60)
    pushed = 0
    for name in ("fullchain.pem", "privkey.pem", "chain.pem", "cert.pem"):
        srcf = Path(cert_dir) / name
        if not srcf.exists():
            continue
        run(["scp", "-i", MAIL_KEY, "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no", str(srcf),
             "%s@%s:%s/%s" % (MAIL_SSH_USER, MAIL_HOST, stage, name)],
            check=False, timeout=120)
        run(ssh + ["docker cp %s/%s %s:%s/%s"
                   % (stage, name, MAIL_CONTAINER, MAIL_CERT_DIR, name)],
            check=False, timeout=60)
        pushed += 1
    run(ssh + ["docker restart " + MAIL_CONTAINER], check=False, timeout=180)
    log.info("mailserver: pushed %d cert file(s) to %s and restarted %s",
             pushed, MAIL_HOST, MAIL_CONTAINER)
