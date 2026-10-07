import json
from datetime import timedelta

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import CredentialForm, PersonForm, SelfContactForm
from .models import AuditEvent, Client, Credential, CredentialType, ImportBatch, Membership, Organization, Person, PrivatePersonnelDetails, Shift, Site
from .personnel_private import decrypt_details, encrypt_details, normalize_ssn
from .services import apply_csv_import, compliance_attendance, personnel_file_bundle, person_snapshot, preview_csv_import, record_person_history, shift_eligibility

TEST_KEY = Fernet.generate_key().decode()
TEST_SSN = "123456789"


@override_settings(PERSONNEL_ENCRYPTION_KEYS=(TEST_KEY,))
class PersonnelFieldsTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(legal_name="Personnel", slug="personnel")
        self.owner = get_user_model().objects.create_user(username="personnel-owner")
        Membership.objects.create(organization=self.org, user=self.owner, role=Membership.Role.OWNER)
        self.person = Person.objects.create(organization=self.org, first_name="Alex", last_name="Guard")
        self.customer = Client.objects.create(organization=self.org, name="Client")
        self.site = Site.objects.create(organization=self.org, client=self.customer, name="Gate", address="1 Example St")
        self.kind = CredentialType.objects.create(organization=self.org, name="Commissioned officer - Level III", code="level-iii")
        self.client.force_login(self.owner)
        self.url = reverse("private_personnel", args=[self.person.pk])

    def save_private(self, **kwargs):
        data = {"action": "save", "ssn": TEST_SSN, "driver_license_number": "TEST-DL-123",
                "driver_license_state": "tx", "reason": "Personnel verification for payroll setup."}
        data.update(kwargs)
        return self.client.post(self.url, data)

    def test_default_identity_and_contact_fields_are_editable_and_historied(self):
        values = {"middle_name": "Morgan", "name_suffix": "Jr", "preferred_name": "AJ",
                  "county": "Dallas", "mailing_address_line1": "PO Box 10", "mailing_address_line2": "",
                  "mailing_city": "Dallas", "mailing_state": "TX", "mailing_postal_code": "75201",
                  "birth_city": "Austin", "birth_state": "TX", "emergency_contact_relationship": "Sibling"}
        for key, value in values.items():
            self.assertIn(key, PersonForm.base_fields)
            setattr(self.person, key, value)
        before = person_snapshot(Person.objects.get(pk=self.person.pk))
        self.person.save()
        changes = record_person_history(self.person, before, self.owner)
        self.assertEqual(self.person.full_name, "Alex Morgan Guard Jr")
        self.assertEqual(changes["preferred_name"]["after"], "AJ")
        self.assertEqual(PersonForm.base_fields["first_name"].label, "Legal first name")
        self.assertEqual(PersonForm.base_fields["last_name"].label, "Legal last name")
        for field in ("county", "mailing_city", "emergency_contact_relationship"):
            self.assertIn(field, SelfContactForm.base_fields)
        for field in ("middle_name", "ssn", "birth_city"):
            self.assertNotIn(field, SelfContactForm.base_fields)
        response = self.client.get(reverse("person_detail", args=[self.person.pk]))
        for value in ("PO Box 10", "Sibling", "Austin", "AJ"):
            self.assertContains(response, value)

    def test_current_assignment_is_derived_from_published_schedule(self):
        now = timezone.now()
        Shift.objects.create(organization=self.org, site=self.site, officer=self.person,
            status=Shift.Status.PUBLISHED, starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1),
            post_name="Lobby patrol")
        response = self.client.get(reverse("person_detail", args=[self.person.pk]))
        self.assertContains(response, "Lobby patrol")
        self.assertContains(response, "1 Example St")
        self.assertContains(response, "not proof of current attendance")

    def test_credentials_record_license_and_weapon_qualifications_and_export(self):
        form = CredentialForm({"person": str(self.person.pk), "credential_type": self.kind.pk,
            "number": "TEST-LICENSE", "status": "active", "issued_on": "2026-01-01",
            "handgun_qualification": "revolver", "shotgun_qualification": "not_qualified"},
            instance=Credential(organization=self.org))
        self.assertTrue(form.is_valid(), form.errors)
        credential = form.save()
        self.assertEqual(credential.handgun_qualification, Credential.Handgun.REVOLVER)
        response = self.client.get(reverse("person_detail", args=[self.person.pk]), {"tab": "credentials"})
        self.assertContains(response, "Revolver only")
        self.assertContains(response, "Not qualified / not patterned")
        bundle = personnel_file_bundle(self.org, self.person, Membership.Role.HR, sensitive_fields=True)
        self.assertIn("revolver", str(bundle))
        self.assertNotIn("license_level", CredentialForm.base_fields)
        self.assertNotIn("license_level", {field.name for field in Credential._meta.fields})
        self.assertNotIn("license_level", str(bundle))
        self.assertNotContains(response, "Level:")
        self.assertContains(response, self.kind.name)
        form = CredentialForm({"person": str(self.person.pk), "credential_type": self.kind.pk,
            "number": "SECOND", "status": "active", "handgun_qualification": "unrecognized"},
            instance=Credential(organization=self.org))
        self.assertFalse(form.is_valid())
        self.assertIn("handgun_qualification", form.errors)

    def test_private_details_encrypted_masked_never_in_profile_history_or_export(self):
        self.assertRedirects(self.save_private(), self.url)
        record = PrivatePersonnelDetails.objects.get(person=self.person)
        self.assertNotIn(TEST_SSN, record.encrypted_payload)
        self.assertNotIn("TEST-DL-123", record.encrypted_payload)
        self.assertEqual(record.ssn_last_four, "6789")
        self.assertEqual(decrypt_details(self.person, record)["ssn"], TEST_SSN)
        response = self.client.get(self.url)
        self.assertContains(response, "***-**-6789")
        self.assertNotContains(response, TEST_SSN)
        self.assertNotContains(response, "123-45-6789")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        public = self.client.get(reverse("person_detail", args=[self.person.pk]))
        self.assertNotContains(public, "TEST-DL-123")
        self.assertNotContains(public, "6789")
        self.assertNotIn("ssn", person_snapshot(self.person))
        self.assertEqual(self.person.history.count(), 0)
        exported = str(personnel_file_bundle(self.org, self.person, Membership.Role.HR, sensitive_fields=True))
        self.assertNotIn(TEST_SSN, exported)
        self.assertNotIn("TEST-DL-123", exported)

    def test_reveal_requires_valid_reason_and_audits_without_plaintext(self):
        self.save_private()
        response = self.client.post(self.url, {"action": "reveal", "reveal-reason": "short"})
        self.assertNotContains(response, "123-45-6789")
        self.assertFalse(AuditEvent.objects.filter(action="person.ssn_revealed").exists())
        purpose = "Verify SSN for authorized payroll onboarding."
        response = self.client.post(self.url, {"action": "reveal", "reveal-reason": purpose})
        self.assertContains(response, "123-45-6789")
        event = AuditEvent.objects.get(action="person.ssn_revealed")
        self.assertNotIn(TEST_SSN, json.dumps(event.metadata))
        self.assertNotIn(purpose, json.dumps(event.metadata))
        self.assertEqual(Fernet(TEST_KEY).decrypt(event.metadata["encrypted_purpose"].encode()).decode(), purpose)
        self.assertNotContains(self.client.get(self.url), "123-45-6789")

    def test_blank_keeps_ssn_and_explicit_clear_removes_it(self):
        self.save_private()
        self.save_private(ssn="", driver_license_number="REPLACED")
        record = PrivatePersonnelDetails.objects.get()
        self.assertEqual(decrypt_details(self.person, record)["ssn"], TEST_SSN)
        self.save_private(ssn="", clear_ssn="on")
        record.refresh_from_db()
        self.assertEqual(record.ssn_last_four, "")
        self.assertEqual(decrypt_details(self.person, record)["ssn"], "")

    def test_invalid_ssn_never_echoed_or_saved_and_clear_conflict_rejected(self):
        for value in ("000-45-6789", "666456789", "912456789", "123006789", "123450000", "abc", "12345678"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    normalize_ssn(value)
        response = self.save_private(ssn="000-45-6789")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'value="000-45-6789"')
        self.assertFalse(PrivatePersonnelDetails.objects.exists())
        response = self.save_private(clear_ssn="on")
        self.assertContains(response, "Choose either replacement or removal")
        self.assertFalse(PrivatePersonnelDetails.objects.exists())

    def test_allowed_roles_and_tenant_restrictions(self):
        self.save_private()
        for role in (Membership.Role.HR, Membership.Role.ADMIN, Membership.Role.OFFICER,
                     Membership.Role.SUPERVISOR, Membership.Role.SCHEDULER, Membership.Role.PAYROLL, Membership.Role.AUDITOR):
            user = get_user_model().objects.create_user(username=f"private-{role}")
            Membership.objects.create(organization=self.org, user=user, role=role)
            self.client.force_login(user)
            expected = 200 if role in (Membership.Role.HR, Membership.Role.ADMIN) else 403
            self.assertEqual(self.client.get(self.url).status_code, expected)
            if expected == 403:
                self.assertEqual(self.client.post(self.url, {"action": "reveal", "reveal-reason": "Attempted other person's SSN"}).status_code, 403)
        other = Organization.objects.create(legal_name="Other", slug="private-other")
        user = get_user_model().objects.create_user(username="foreign-private-owner")
        Membership.objects.create(organization=other, user=user, role=Membership.Role.OWNER)
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_missing_or_incorrect_key_fails_closed_without_overwriting(self):
        self.save_private()
        record = PrivatePersonnelDetails.objects.get()
        encrypted = record.encrypted_payload
        for keys in ((), ("not-a-key",), (Fernet.generate_key().decode(),)):
            with override_settings(PERSONNEL_ENCRYPTION_KEYS=keys):
                response = self.save_private(ssn="234567890")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "No restricted data can be edited or revealed")
                self.assertNotContains(response, "TEST-DL-123")
                record.refresh_from_db()
                self.assertEqual(record.encrypted_payload, encrypted)

    def test_key_rotation_and_tenant_bound_payload(self):
        self.save_private()
        record = PrivatePersonnelDetails.objects.get()
        new_key = Fernet.generate_key().decode()
        with override_settings(PERSONNEL_ENCRYPTION_KEYS=(new_key, TEST_KEY)):
            self.assertEqual(decrypt_details(self.person, record)["ssn"], TEST_SSN)
            rotated = encrypt_details(self.person, decrypt_details(self.person, record))
            self.assertIn(TEST_SSN, Fernet(new_key).decrypt(rotated.encode()).decode())
        other = Person.objects.create(organization=self.org, first_name="Other", last_name="Record")
        with self.assertRaises(ValidationError):
            decrypt_details(other, record)

    def test_public_csv_import_fields_and_block_private_identifiers_before_staging(self):
        upload = SimpleUploadedFile("person.csv", b"first_name,last_name,email,middle_name,preferred_name,county,mailing_city,birth_city,emergency_contact_relationship\nSam,Worker,sam@example.com,Pat,SJ,Dallas,Austin,Houston,Parent\n")
        batch, _ = preview_csv_import(organization=self.org, entity="people", upload=upload, actor=self.owner)
        self.assertEqual(apply_csv_import(batch, self.owner), 1)
        person = Person.objects.get(email="sam@example.com")
        self.assertEqual((person.middle_name, person.preferred_name, person.birth_city), ("Pat", "SJ", "Houston"))
        initial = ImportBatch.objects.count()
        for header in ("ssn", "Social Security Number", "driver_license_number",
                       "Driver's License Number", "Drivers Licence State", "DL State"):
            upload = SimpleUploadedFile("sensitive.csv", f"first_name,last_name,email,{header}\nSam,Worker,sam@example.com,{TEST_SSN}\n".encode())
            with self.assertRaises(ValidationError):
                preview_csv_import(organization=self.org, entity="people", upload=upload, actor=self.owner)
        self.assertEqual(ImportBatch.objects.count(), initial)

    def test_private_forms_are_accessible_and_no_background_or_drug_duplicates(self):
        from .test_form_ui import FormControlParser
        self.save_private()
        response = self.client.get(self.url)
        parser = FormControlParser()
        parser.feed(response.content.decode())
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        for attrs, wrapped in parser.controls:
            self.assertTrue(wrapped or attrs.get("id") in parser.labels, attrs)
        self.assertNotContains(response, "Background-check confirmation")
        self.assertNotContains(response, "last_drug_test_result")

    def test_plaintext_cannot_be_leaked_into_audit_purpose_and_reveal_requires_csrf(self):
        from django.test import Client as TestClient
        self.save_private()
        response = self.client.post(self.url, {"action": "reveal", "reveal-reason": f"Verify test identifier {TEST_SSN} for payroll"})
        self.assertEqual(response.status_code, 200)
        for event in AuditEvent.objects.filter(action__startswith="person.private"):
            self.assertNotIn(TEST_SSN, json.dumps(event.metadata))
        reveal = AuditEvent.objects.get(action="person.ssn_revealed")
        self.assertNotIn(TEST_SSN, json.dumps(reveal.metadata))
        csrf_client = TestClient(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        response = csrf_client.post(self.url, {"action": "reveal", "reveal-reason": "Authorized verification purpose"})
        self.assertEqual(response.status_code, 403)

    def test_credential_csv_qualification_columns_validate_and_preserve_blank_unknown(self):
        self.person.email = "guard@example.com"
        self.person.save()
        upload = SimpleUploadedFile("credential.csv", b"person_email,type_code,status,number,handgun_qualification,shotgun_qualification\nguard@example.com,level-iii,active,TEST-REG,semi_auto,qualified\n")
        batch, _ = preview_csv_import(organization=self.org, entity="credentials", upload=upload, actor=self.owner)
        self.assertFalse(batch.errors)
        apply_csv_import(batch, self.owner)
        credential = Credential.objects.get()
        self.assertEqual((credential.credential_type, credential.handgun_qualification, credential.shotgun_qualification),
                         (self.kind, "semi_auto", "qualified"))
        upload = SimpleUploadedFile("invalid.csv", b"person_email,type_code,status,handgun_qualification\nguard@example.com,level-iii,active,pistol\n")
        batch, _ = preview_csv_import(organization=self.org, entity="credentials", upload=upload, actor=self.owner)
        self.assertTrue(batch.errors)
        with self.assertRaises(ValidationError):
            apply_csv_import(batch, self.owner)
        self.assertEqual(Credential._meta.get_field("shotgun_qualification").get_default(), "")

    def test_termination_requires_date_and_rehire_is_optional(self):
        data = {"first_name": "Alex", "last_name": "Guard", "status": "terminated", "state": "TX"}
        form = PersonForm(data, instance=self.person)
        self.assertFalse(form.is_valid())
        self.assertIn("termination_date", form.errors)
        data["termination_date"] = timezone.localdate().isoformat()
        for selection, expected in (("unknown", None), ("true", True), ("false", False)):
            with self.subTest(selection=selection):
                form = PersonForm(data | {"eligible_for_rehire": selection}, instance=self.person)
                self.assertTrue(form.is_valid(), form.errors)
                person = form.save()
                self.assertIs(person.eligible_for_rehire, expected)
        self.assertNotIn("eligible_for_rehire", SelfContactForm.base_fields)
        self.assertNotIn("termination_date", SelfContactForm.base_fields)
        form = PersonForm({"first_name": "Alex", "last_name": "Guard", "status": "active", "state": "TX"}, instance=self.person)
        self.assertTrue(form.is_valid(), form.errors)

    def test_termination_edit_preserves_access_and_records_history_and_directory(self):
        self.person.user = self.owner
        self.person.save()
        response = self.client.post(reverse("person_edit", args=[self.person.pk]), {
            "first_name": "Alex", "last_name": "Guard", "status": "terminated", "state": "TX"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter the termination date")
        self.person.refresh_from_db()
        self.assertEqual(self.person.status, Person.Status.ONBOARDING)
        response = self.client.post(reverse("person_edit", args=[self.person.pk]), {
            "first_name": "Alex", "last_name": "Guard", "status": "terminated", "state": "TX",
            "termination_date": timezone.localdate().isoformat(), "eligible_for_rehire": "false"})
        self.assertRedirects(response, reverse("person_detail", args=[self.person.pk]))
        self.person.refresh_from_db()
        self.assertEqual(self.person.status, Person.Status.TERMINATED)
        self.assertIs(self.person.eligible_for_rehire, False)
        membership = Membership.objects.get(user=self.owner, organization=self.org)
        self.assertTrue(membership.active)
        self.assertEqual(membership.role, Membership.Role.OWNER)
        changes = self.person.history.latest("created_at").changes
        self.assertIn("termination_date", changes)
        self.assertEqual(changes["eligible_for_rehire"]["after"], "False")
        response = self.client.get(reverse("person_detail", args=[self.person.pk]))
        self.assertContains(response, "Terminated")
        self.assertContains(response, "Eligible for rehire")
        response = self.client.get(reverse("people"), {"status": "terminated"})
        self.assertContains(response, "Alex Guard")
        bundle = personnel_file_bundle(self.org, self.person, Membership.Role.HR, sensitive_fields=True)
        self.assertIn("terminated", str(bundle))
        self.assertIn("eligible_for_rehire", str(bundle))

    def test_terminated_employee_not_in_operational_compliance_or_schedule_candidates(self):
        now = timezone.now()
        shift = Shift.objects.create(organization=self.org, site=self.site, officer=self.person,
            starts_at=now + timedelta(hours=1), ends_at=now + timedelta(hours=9))
        self.person.status = Person.Status.TERMINATED
        self.person.termination_date = timezone.localdate()
        self.person.save()
        allowed, reasons = shift_eligibility(shift, self.person)
        self.assertFalse(allowed)
        self.assertIn("Officer is terminated.", reasons)
        from .scope import ActorScope
        scope = ActorScope(Membership.objects.get(organization=self.org, user=self.owner))
        built = compliance_attendance(self.org, scope)
        self.assertNotIn(self.person.pk, {row["person"].pk for row in built["credentials"]["rows"]})
        from .forms import ShiftForm
        from .views import _scope_querysets
        form = _scope_querysets(ShiftForm(), self.org, scope)
        self.assertFalse(form.fields["officer"].queryset.filter(pk=self.person.pk).exists())
        form = _scope_querysets(ShiftForm(instance=shift), self.org, scope)
        self.assertTrue(form.fields["officer"].queryset.filter(pk=self.person.pk).exists())

    def test_csv_termination_date_and_optional_rehire_validation(self):
        for status, ended, rehire in (("terminated", "", ""), ("terminated", "invalid", "Yes"),
                                      ("terminated", "2026-10-07", "maybe"), ("not-a-status", "", "")):
            upload = SimpleUploadedFile("invalid.csv",
                f"first_name,last_name,email,status,termination_date,eligible_for_rehire\nAlex,Guard,import@example.com,{status},{ended},{rehire}\n".encode())
            batch, _ = preview_csv_import(organization=self.org, entity="people", upload=upload, actor=self.owner)
            self.assertTrue(batch.errors)
            with self.assertRaises(ValidationError):
                apply_csv_import(batch, self.owner)
        for value, expected in (("Yes", True), ("No", False), ("", None)):
            upload = SimpleUploadedFile("terminated.csv",
                f"first_name,last_name,email,status,termination_date,eligible_for_rehire\nAlex,Guard,import@example.com,terminated,2026-10-07,{value}\n".encode())
            batch, _ = preview_csv_import(organization=self.org, entity="people", upload=upload, actor=self.owner)
            self.assertFalse(batch.errors)
            apply_csv_import(batch, self.owner)
            person = Person.objects.get(email="import@example.com")
            self.assertIs(person.eligible_for_rehire, expected)
            self.assertEqual(person.status, Person.Status.TERMINATED)
        person.eligible_for_rehire = True
        person.save()
        upload = SimpleUploadedFile("no-rehire-column.csv",
            b"first_name,last_name,email,status,termination_date\nAlex,Guard,import@example.com,terminated,2026-10-07\n")
        batch, _ = preview_csv_import(organization=self.org, entity="people", upload=upload, actor=self.owner)
        apply_csv_import(batch, self.owner)
        person.refresh_from_db()
        self.assertIs(person.eligible_for_rehire, True)
