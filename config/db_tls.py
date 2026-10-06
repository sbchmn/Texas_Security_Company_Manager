"""TLS options for the MySQL connection, read from the environment.

Left alone, PyMySQL runs in MySQL's ``preferred`` mode: it encrypts when the server offers TLS but
accepts any certificate, so a managed database (DigitalOcean enforces TLS) connects but cannot be told
apart from an impostor. Configuring the provider's CA turns on verification.

``MYSQL_SSL_MODE`` follows MySQL's own client names:

* ``disabled`` — never encrypt.
* ``preferred`` — encrypt when offered, no certificate checks (the default when no CA is configured).
* ``required`` — as ``preferred``, but refuse a server that does not offer TLS.
* ``verify_ca`` — encrypt and require a certificate signed by the configured CA.
* ``verify_identity`` — as ``verify_ca``, and the certificate must also name ``MYSQL_HOST``
  (the default when a CA is configured).

The CA comes from ``MYSQL_SSL_CA`` (a file path) or ``MYSQL_SSL_CA_PEM`` (the certificate text, which is
how App Platform hands over ``${db.CA_CERT}``). PyMySQL loads a CA from a file only, so the text is
written once to ``MYSQL_SSL_CA_DIR`` (default: the system temp directory) under a name derived from its
contents.
"""
import hashlib
import os
import tempfile

from django.core.exceptions import ImproperlyConfigured

MODES = ("disabled", "preferred", "required", "verify_ca", "verify_identity")


def _pem_file(pem, directory=None):
    pem = pem.replace("\\n", "\n").strip() + "\n"
    if "-----BEGIN CERTIFICATE-----" not in pem:
        raise ImproperlyConfigured("MYSQL_SSL_CA_PEM does not contain a PEM certificate "
                                   "(expected a -----BEGIN CERTIFICATE----- block).")
    directory = directory or tempfile.gettempdir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"mysql-ca-{hashlib.sha256(pem.encode()).hexdigest()[:16]}.pem")
    if not os.path.exists(path):
        partial = f"{path}.{os.getpid()}.tmp"
        with open(partial, "w", encoding="ascii") as handle:
            handle.write(pem)
        os.replace(partial, path)
    return path


def mysql_tls_options(env=None):
    """Connection OPTIONS that apply ``MYSQL_SSL_MODE``; empty for PyMySQL's own ``preferred`` mode."""
    env = os.environ if env is None else env
    ca = (env.get("MYSQL_SSL_CA") or "").strip()
    pem = (env.get("MYSQL_SSL_CA_PEM") or "").strip()
    if ca and pem:
        raise ImproperlyConfigured("Set MYSQL_SSL_CA or MYSQL_SSL_CA_PEM, not both.")
    if pem:
        ca = _pem_file(pem, (env.get("MYSQL_SSL_CA_DIR") or "").strip() or None)
    elif ca and not os.path.isfile(ca):
        raise ImproperlyConfigured(f"MYSQL_SSL_CA points at {ca}, which is not a file.")
    mode = (env.get("MYSQL_SSL_MODE") or "").strip().lower().replace("-", "_")
    if not mode:
        mode = "verify_identity" if ca else "preferred"
    if mode not in MODES:
        raise ImproperlyConfigured(f"MYSQL_SSL_MODE must be one of {', '.join(MODES)}; got {mode!r}.")
    if mode == "disabled":
        return {"ssl_disabled": True}
    if mode == "preferred":
        return {}
    if mode == "required":
        return {"ssl": {"verify_mode": False, "check_hostname": False}}
    if not ca:
        raise ImproperlyConfigured(f"MYSQL_SSL_MODE={mode} needs MYSQL_SSL_CA or MYSQL_SSL_CA_PEM.")
    return {"ssl": {"ca": ca, "verify_mode": True, "check_hostname": mode == "verify_identity"}}
