import os
import ssl
import tempfile

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from pymysql.connections import Connection

from config.db_tls import mysql_tls_options

PEM = ("-----BEGIN CERTIFICATE-----\n"
       "MIIBszCCAVmgAwIBAgIUQ0FUZXN0Q0FUZXN0Q0FUZXN0Q0FUZXN0MAoGCCqGSM49BAMC\n"
       "-----END CERTIFICATE-----")


class MysqlTlsOptionsTest(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ca = os.path.join(self.dir.name, "ca.crt")
        with open(self.ca, "w") as handle:
            handle.write(PEM)

    def test_nothing_configured_keeps_pymysql_preferred_mode(self):
        self.assertEqual({}, mysql_tls_options({}))
        self.assertEqual({}, mysql_tls_options({"MYSQL_SSL_MODE": "preferred", "MYSQL_SSL_CA": self.ca}))

    def test_disabled_really_turns_tls_off(self):
        self.assertEqual({"ssl_disabled": True}, mysql_tls_options({"MYSQL_SSL_MODE": "disabled"}))

    def test_a_ca_defaults_to_full_verification(self):
        self.assertEqual({"ssl": {"ca": self.ca, "verify_mode": True, "check_hostname": True}},
                         mysql_tls_options({"MYSQL_SSL_CA": self.ca}))

    def test_verify_ca_skips_only_the_hostname_check(self):
        options = mysql_tls_options({"MYSQL_SSL_CA": self.ca, "MYSQL_SSL_MODE": "VERIFY-CA"})
        self.assertEqual({"ssl": {"ca": self.ca, "verify_mode": True, "check_hostname": False}}, options)

    def test_required_encrypts_without_a_ca(self):
        self.assertEqual({"ssl": {"verify_mode": False, "check_hostname": False}},
                         mysql_tls_options({"MYSQL_SSL_MODE": "required"}))

    def test_pem_text_is_written_once_to_a_stable_file(self):
        env = {"MYSQL_SSL_CA_PEM": PEM.replace("\n", "\\n"), "MYSQL_SSL_CA_DIR": self.dir.name}
        first = mysql_tls_options(env)["ssl"]["ca"]
        self.assertEqual(first, mysql_tls_options(env)["ssl"]["ca"])
        with open(first) as handle:
            self.assertEqual(PEM + "\n", handle.read())

    def test_misconfiguration_fails_at_startup_with_a_clear_message(self):
        for env, fragment in (
                ({"MYSQL_SSL_MODE": "verify_identity"}, "needs MYSQL_SSL_CA"),
                ({"MYSQL_SSL_MODE": "sometimes"}, "must be one of"),
                ({"MYSQL_SSL_CA": os.path.join(self.dir.name, "missing.crt")}, "not a file"),
                ({"MYSQL_SSL_CA_PEM": "not a cert", "MYSQL_SSL_CA_DIR": self.dir.name}, "PEM certificate"),
                ({"MYSQL_SSL_CA": self.ca, "MYSQL_SSL_CA_PEM": PEM}, "not both")):
            with self.subTest(env=env), self.assertRaisesMessage(ImproperlyConfigured, fragment):
                mysql_tls_options(env)

    def test_pymysql_builds_the_intended_contexts(self):
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "test CA")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now)
                .not_valid_after(now + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        env = {"MYSQL_SSL_CA_PEM": cert.public_bytes(serialization.Encoding.PEM).decode(),
               "MYSQL_SSL_CA_DIR": self.dir.name}
        build = lambda extra: Connection._create_ssl_ctx(None, mysql_tls_options({**env, **extra})["ssl"])
        cases = {"verify_identity": (ssl.CERT_REQUIRED, True), "verify_ca": (ssl.CERT_REQUIRED, False),
                 "required": (ssl.CERT_NONE, False)}
        for mode, expected in cases.items():
            context = build({"MYSQL_SSL_MODE": mode})
            with self.subTest(mode=mode):
                self.assertEqual(expected, (context.verify_mode, context.check_hostname))
        self.assertEqual(1, len(build({}).get_ca_certs()), "the configured CA is the one trusted")
