import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from .models import Membership, Organization, Person


class PwaRolloutTest(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="pwa-owner")
        self.org = Organization.objects.create(legal_name="PWA", display_name="First membership", slug="pwa")
        Membership.objects.create(user=self.owner, organization=self.org, role="owner")
        self.person = Person.objects.create(organization=self.org, user=self.owner, first_name="PWA", last_name="Officer")
        self.client.force_login(self.owner)

    @override_settings(PWA_NAME="TSCM - Texas Liberty Coalition", PWA_SHORT_NAME="TSCM")
    def test_install_identity_stable_without_login_or_tenant_selection(self):
        expected = self.client.get(reverse("manifest")).json()
        self.client.logout()
        anonymous = self.client.get(reverse("manifest"))
        self.assertEqual(expected, anonymous.json())
        self.assertEqual("application/manifest+json", anonymous["Content-Type"])
        self.assertEqual("TSCM - Texas Liberty Coalition", expected["name"])
        self.assertEqual("TSCM", expected["short_name"])
        self.assertEqual("/", expected["id"])
        self.assertEqual("/", expected["scope"])
        self.assertEqual("/?source=pwa", expected["start_url"])
        self.assertIn("no-cache", anonymous["Cache-Control"])

    def test_mobile_icons_exist_and_are_opaque_correct_sizes(self):
        manifest = self.client.get(reverse("manifest")).json()
        self.assertEqual({"192x192", "512x512"}, {icon["sizes"] for icon in manifest["icons"]})
        self.assertTrue(any(icon["purpose"] == "maskable" for icon in manifest["icons"]))
        for size in (180, 192, 512, 1024):
            with Image.open(settings.BASE_DIR / "static" / "icons" / f"icon-{size}.png") as image:
                self.assertEqual((size, size), image.size)
                self.assertEqual("RGB", image.mode)
        with Image.open(settings.BASE_DIR / "static" / "icons" / "icon-maskable-512.png") as image:
            self.assertEqual((512, 512), image.size)
        with Image.open(settings.BASE_DIR / "mobile" / "ios" / "TSCM" / "Assets.xcassets" /
                        "AppIcon.appiconset" / "AppIcon.png") as image:
            self.assertEqual((1024, 1024), image.size)

    def test_ios_tags_and_install_help_discoverable(self):
        page = self.client.get(reverse("my_account"))
        self.assertContains(page, reverse("install_app"))
        self.assertContains(page, 'rel="apple-touch-icon"')
        self.assertContains(page, "apple-mobile-web-app-capable")
        self.assertContains(page, "js/pwa.js")
        help_page = self.client.get(reverse("install_app"))
        for text in ("Android", "iPhone", "Add to Home Screen", "12 hours", "personal device",
                     "clearing browser data", "data-pwa-install"):
            self.assertContains(help_page, text)
        self.client.logout()
        self.assertEqual(200, self.client.get(reverse("install_app")).status_code)

    @override_settings(ANDROID_SHA256_FINGERPRINTS=[])
    def test_no_trusted_android_certificate_published_by_default(self):
        self.client.logout()
        response = self.client.get(reverse("asset_links"))
        self.assertEqual(200, response.status_code)
        self.assertEqual([], response.json())

    @override_settings(ANDROID_PACKAGE_ID="com.texaslibertycoalition.tscm",
                       ANDROID_SHA256_FINGERPRINTS=[":".join(["AB"] * 32), ":".join(["CD"] * 32)])
    def test_public_digital_asset_links_contains_only_explicit_certificates(self):
        self.client.logout()
        link = self.client.get(reverse("asset_links")).json()[0]
        self.assertEqual(["delegate_permission/common.handle_all_urls"], link["relation"])
        self.assertEqual("android_app", link["target"]["namespace"])
        self.assertEqual("com.texaslibertycoalition.tscm", link["target"]["package_name"])
        self.assertEqual(2, len(link["target"]["sha256_cert_fingerprints"]))
        self.assertNotIn("user", link)

    def test_worker_is_public_uncached_script_with_hashed_static_urls(self):
        self.client.logout()
        response = self.client.get(reverse("service_worker"))
        self.assertEqual(200, response.status_code)
        self.assertEqual("application/javascript", response["Content-Type"])
        self.assertEqual("/", response["Service-Worker-Allowed"])
        self.assertEqual("no-cache", response["Cache-Control"])
        body = response.content.decode()
        self.assertNotIn("{{", body)
        self.assertIn("X-TSCM-Offline-Clock", body)
        self.assertIn('url.pathname==="/"', body)
        self.assertIn("data-clock-stale", body)
        self.assertIn('name.startsWith("tscm-shell-")', body)
        self.assertNotIn("skipWaiting", body)
        self.assertNotIn('"/accounts/login/"', body)

    def test_only_real_officer_clock_carries_cache_marker(self):
        self.assertEqual("1", self.client.get(reverse("clock"))["X-TSCM-Offline-Clock"])
        self.person.delete()
        self.assertNotIn("X-TSCM-Offline-Clock", self.client.get(reverse("clock")))
        self.client.logout()
        self.assertNotIn("X-TSCM-Offline-Clock", self.client.get(reverse("clock")))

    def test_mobile_package_identity_matches_approved_origin(self):
        android = json.loads((settings.BASE_DIR / "mobile" / "android" / "release-config.json").read_text())
        self.assertEqual("https://tscm.texaslibertycoalition.com", android["origin"])
        self.assertEqual("com.texaslibertycoalition.tscm", android["packageId"])
        self.assertEqual("TSCM - Texas Liberty Coalition", android["appName"])
        ios = (settings.BASE_DIR / "mobile" / "ios" / "TSCM" / "TSCMApp.swift").read_text()
        self.assertIn(android["origin"], ios)
        self.assertIn("limitsNavigationsToAppBoundDomains = true", ios)
        self.assertNotIn("NSAllowsArbitraryLoads", (settings.BASE_DIR / "mobile" / "ios" / "project.yml").read_text())
