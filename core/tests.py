from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from .models import AuditEvent, Branch, Checkpoint, Client, CustomFieldDefinition, Membership, OfflineClockDevice, Organization, PayrollRun, Person, PersonCustomValue, Punch, PunchAdjustment, Shift, Site

class AppTestCase(TestCase):
    def setUp(self):
        User=get_user_model()
        self.owner=User.objects.create_user(username="owner@example.com", password="correct horse battery staple")
        self.officer=User.objects.create_user(username="officer@example.com", password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Acme Security LLC", display_name="Acme Security", slug="acme-security")
        self.other_org=Organization.objects.create(legal_name="Other LLC", display_name="Other", slug="other")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.Role.OWNER)
        Membership.objects.create(user=self.officer, organization=self.org, role=Membership.Role.OFFICER)
        self.branch=Branch.objects.create(organization=self.org, name="Austin")
        Person.objects.create(organization=self.other_org, first_name="Hidden", last_name="Person")

    def test_health_is_public(self):
        response=self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_dashboard_requires_login(self):
        response=self.client.get(reverse("dashboard"))
        self.assertRedirects(response, "/accounts/login/?next=/")

    def test_people_are_tenant_scoped(self):
        Person.objects.create(organization=self.org, first_name="Visible", last_name="Officer", branch=self.branch)
        self.client.force_login(self.owner)
        response=self.client.get(reverse("people"))
        self.assertContains(response, "Visible Officer")
        self.assertNotContains(response, "Hidden Person")

    def test_officer_cannot_change_branding(self):
        self.client.force_login(self.officer)
        self.assertEqual(self.client.get(reverse("branding")).status_code, 403)

    def test_brand_update_is_audited(self):
        self.client.force_login(self.owner)
        response=self.client.post(reverse("branding"), {"display_name":"Acme Command", "primary_color":"#112233", "accent_color":"#22AA99", "support_email":"help@example.com", "support_phone":"", "timezone":"America/Chicago"})
        self.assertRedirects(response, reverse("branding"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.display_name, "Acme Command")
        self.assertTrue(AuditEvent.objects.filter(organization=self.org, action="branding.updated").exists())

    def test_invalid_brand_color_is_rejected(self):
        self.org.primary_color="red"
        with self.assertRaises(ValidationError): self.org.full_clean()

    def test_audit_events_are_append_only(self):
        event=AuditEvent.objects.create(organization=self.org, actor=self.owner, action="test", target_type="organization", target_id=str(self.org.pk))
        event.action="changed"
        with self.assertRaises(ValueError): event.save()
        with self.assertRaises(ValueError): event.delete()
        with self.assertRaises(ValueError): AuditEvent.objects.filter(pk=event.pk).update(action="changed")
        with self.assertRaises(ValueError): AuditEvent.objects.filter(pk=event.pk).delete()

class UploadValidationTest(TestCase):
    def test_oversized_brand_logo_is_rejected(self):
        from io import BytesIO
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .forms import BrandForm
        content = BytesIO()
        Image.new("RGB", (1400, 1400), "white").save(content, format="BMP")
        upload = SimpleUploadedFile("logo.bmp", content.getvalue(), content_type="image/bmp")
        form = BrandForm(data={"display_name":"Acme", "primary_color":"#112233", "accent_color":"#22AA99", "support_email":"", "support_phone":"", "timezone":"America/Chicago"}, files={"logo": upload})
        self.assertFalse(form.is_valid())
        self.assertIn("5 MiB", str(form.errors["logo"]))

class CompletedMvpSecurityTest(TestCase):
    def setUp(self):
        User=get_user_model();self.user=User.objects.create_user(username="guard",password="secret")
        self.org=Organization.objects.create(legal_name="Secure",display_name="Secure",slug="secure")
        Membership.objects.create(organization=self.org,user=self.user,role=Membership.Role.OFFICER)
        self.person=Person.objects.create(organization=self.org,user=self.user,first_name="G",last_name="One")

    def test_audit_hash_chain_detects_raw_tampering(self):
        from django.db import connection
        from .services import verify_audit_chain
        event=AuditEvent.objects.create(organization=self.org,actor=self.user,action="created",target_type="person",target_id=str(self.person.pk),metadata={"safe":"yes"})
        self.assertEqual(verify_audit_chain(self.org),[])
        with connection.cursor() as cursor:cursor.execute("UPDATE core_auditevent SET metadata=%s WHERE id=%s",['{\"safe\":\"no\"}',event.pk.hex])
        self.assertEqual(verify_audit_chain(self.org),[str(event.pk)])

    def test_offline_token_is_tenant_bound_replay_safe_and_idempotent(self):
        from django.core import signing
        from django.utils import timezone
        device=OfflineClockDevice.objects.create(organization=self.org,person=self.person,user=self.user)
        token=signing.dumps({"device":str(device.pk),"organization":str(self.org.pk),"user":self.user.pk},salt="offline-clock")
        payload={"device_token":token,"device_sequence":1,"client_event_id":str(__import__('uuid').uuid4()),"kind":"in","occurred_at":timezone.now().isoformat(),"offline":True}
        response=self.client.post(reverse("offline_punch_sync"),data=__import__('json').dumps(payload),content_type="application/json")
        self.assertEqual(response.status_code,201)
        self.assertEqual(self.client.post(reverse("offline_punch_sync"),data=__import__('json').dumps(payload),content_type="application/json").status_code,200)
        payload["client_event_id"]=str(__import__('uuid').uuid4())
        self.assertEqual(self.client.post(reverse("offline_punch_sync"),data=__import__('json').dumps(payload),content_type="application/json").status_code,400)

    def test_checkpoint_rejects_wrong_site(self):
        from django.core import signing
        from django.utils import timezone
        client=Client.objects.create(organization=self.org,name="Client");site=Site.objects.create(organization=self.org,client=client,name="A",address="A");other=Site.objects.create(organization=self.org,client=client,name="B",address="B")
        shift=Shift.objects.create(organization=self.org,site=site,officer=self.person,starts_at=timezone.now(),ends_at=timezone.now()+__import__('datetime').timedelta(hours=8))
        checkpoint=Checkpoint.objects.create(organization=self.org,site=other,name="Door")
        device=OfflineClockDevice.objects.create(organization=self.org,person=self.person,user=self.user);token=signing.dumps({"device":str(device.pk),"organization":str(self.org.pk),"user":self.user.pk},salt="offline-clock")
        payload={"device_token":token,"device_sequence":1,"client_event_id":str(__import__('uuid').uuid4()),"kind":"checkpoint","occurred_at":timezone.now().isoformat(),"offline":True,"shift_id":str(shift.pk),"checkpoint_code":str(checkpoint.scan_code)}
        self.assertEqual(self.client.post(reverse("offline_punch_sync"),data=__import__('json').dumps(payload),content_type="application/json").status_code,400)

    def test_custom_person_values_cannot_cross_tenants(self):
        other=Organization.objects.create(legal_name="Other",display_name="Other",slug="secure-other")
        definition=CustomFieldDefinition.objects.create(organization=other,name="Secret",key="secret")
        value=PersonCustomValue(organization=self.org,person=self.person,definition=definition,value="x")
        with self.assertRaises(ValidationError):value.full_clean()


class ProductionConfigurationTest(SimpleTestCase):
    def test_missing_production_secret_fails_closed(self):
        import os
        import subprocess
        import sys
        env = os.environ.copy()
        for key in ("SECRET_KEY", "DEBUG", "ALLOWED_HOSTS"):
            env.pop(key, None)
        result = subprocess.run(
            [sys.executable, "-c", "import config.settings"],
            cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECRET_KEY is required", result.stderr)

class AuthenticationSecurityTest(TestCase):
    def test_login_attempts_are_rate_limited(self):
        from django.core.cache import cache
        cache.clear()
        for _ in range(5):
            response=self.client.post(reverse("account_login"),{"login":"target@example.com","password":"wrong"})
            self.assertEqual(response.status_code,200)
        blocked=self.client.post(reverse("account_login"),{"login":"target@example.com","password":"wrong"})
        self.assertEqual(blocked.status_code,429)
        self.assertEqual(blocked.headers["Retry-After"],"900")

class WorkforceServiceTest(TestCase):
    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Client, CredentialType, Site, Shift, TimePolicy
        User=get_user_model(); self.user=User.objects.create_user(username="manager@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Workforce LLC",display_name="Workforce",slug="workforce")
        Membership.objects.create(user=self.user,organization=self.org,role=Membership.Role.ADMIN)
        self.person=Person.objects.create(organization=self.org,first_name="Jordan",last_name="Lee",status=Person.Status.ACTIVE)
        self.client_record=Client.objects.create(organization=self.org,name="Hospital")
        self.site=Site.objects.create(organization=self.org,client=self.client_record,name="North Campus",address="Austin",latitude="30.267200",longitude="-97.743100",geofence_radius_meters=200)
        self.credential_type=CredentialType.objects.create(organization=self.org,name="Commissioned Security Officer",code="commissioned")
        self.shift=Shift.objects.create(organization=self.org,site=self.site,officer=self.person,starts_at=timezone.now()-timedelta(hours=1),ends_at=timezone.now()+timedelta(hours=7),status=Shift.Status.PUBLISHED)
        self.shift.required_credentials.add(self.credential_type)
        TimePolicy.objects.create(organization=self.org,rounding_mode=TimePolicy.RoundingMode.NEAREST,rounding_minutes=15)

    def test_missing_required_credential_blocks_shift(self):
        from .services import shift_eligibility
        allowed,reasons=shift_eligibility(self.shift)
        self.assertFalse(allowed); self.assertIn("missing", reasons[0])

    def test_expired_credential_blocks_clock_in(self):
        import uuid
        from datetime import timedelta
        from django.core.exceptions import ValidationError
        from django.utils import timezone
        from .models import Credential, Punch
        from .services import record_punch
        Credential.objects.create(organization=self.org,person=self.person,credential_type=self.credential_type,status=Credential.Status.ACTIVE,expires_on=timezone.localdate()-timedelta(days=1))
        with self.assertRaisesMessage(ValidationError,"expired"):
            record_punch(organization=self.org,person=self.person,shift=self.shift,client_event_id=uuid.uuid4(),kind=Punch.Kind.IN,occurred_at=timezone.now(),latitude=self.site.latitude,longitude=self.site.longitude)

    def test_punch_is_idempotent_and_outside_geofence_needs_review(self):
        import uuid
        from django.utils import timezone
        from .models import Credential, Punch
        from .services import record_punch
        Credential.objects.create(organization=self.org,person=self.person,credential_type=self.credential_type,status=Credential.Status.ACTIVE,expires_on=timezone.localdate().replace(year=timezone.localdate().year+1))
        event_id=uuid.uuid4()
        punch,created=record_punch(organization=self.org,person=self.person,shift=self.shift,client_event_id=event_id,kind=Punch.Kind.IN,occurred_at=timezone.now(),latitude="31.000000",longitude="-98.000000")
        duplicate,created_again=record_punch(organization=self.org,person=self.person,shift=self.shift,client_event_id=event_id,kind=Punch.Kind.IN,occurred_at=timezone.now(),latitude="31.000000",longitude="-98.000000")
        self.assertTrue(created); self.assertFalse(created_again); self.assertEqual(punch.pk,duplicate.pk); self.assertEqual(punch.review_status,Punch.Review.PENDING)

    def test_payroll_rounding_preserves_calculated_totals(self):
        import uuid
        from datetime import timedelta
        from decimal import Decimal
        from django.utils import timezone
        from .models import Punch
        from .services import payroll_rows
        start=timezone.now().replace(hour=0,minute=0,second=0,microsecond=0)
        Punch.objects.create(organization=self.org,person=self.person,client_event_id=uuid.uuid4(),kind=Punch.Kind.IN,occurred_at=start+timedelta(hours=8))
        Punch.objects.create(organization=self.org,person=self.person,client_event_id=uuid.uuid4(),kind=Punch.Kind.OUT,occurred_at=start+timedelta(hours=16,minutes=7))
        rows=payroll_rows(self.org,start,start+timedelta(days=1))
        self.assertEqual(rows[0]["total_hours"],Decimal("8.00"))

class WorkforceViewTest(TestCase):
    def setUp(self):
        from django.utils import timezone
        from datetime import timedelta
        from .models import Client, Credential, CredentialType, Site, Shift
        User=get_user_model(); self.user=User.objects.create_user(username="officer2@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Portal LLC",display_name="Portal",slug="portal")
        Membership.objects.create(user=self.user,organization=self.org,role=Membership.Role.OFFICER)
        self.person=Person.objects.create(organization=self.org,user=self.user,first_name="Taylor",last_name="Reed",status=Person.Status.ACTIVE)
        client=Client.objects.create(organization=self.org,name="Warehouse")
        self.site=Site.objects.create(organization=self.org,client=client,name="Dock",address="Dallas",latitude="32.776700",longitude="-96.797000",geofence_radius_meters=200)
        credential_type=CredentialType.objects.create(organization=self.org,name="Unarmed Officer",code="unarmed")
        Credential.objects.create(organization=self.org,person=self.person,credential_type=credential_type,status=Credential.Status.ACTIVE,expires_on=timezone.localdate()+timedelta(days=365))
        self.shift=Shift.objects.create(organization=self.org,site=self.site,officer=self.person,starts_at=timezone.now()-timedelta(hours=1),ends_at=timezone.now()+timedelta(hours=7),status=Shift.Status.PUBLISHED)
        self.shift.required_credentials.add(credential_type)
        self.client.force_login(self.user)

    def test_officer_can_open_clock_but_not_management_schedule(self):
        self.assertEqual(self.client.get(reverse("clock")).status_code,200)
        self.assertEqual(self.client.get(reverse("schedule")).status_code,403)

    def test_punch_api_records_and_deduplicates_event(self):
        import json,uuid
        from django.utils import timezone
        event_id=str(uuid.uuid4())
        payload={"client_event_id":event_id,"kind":"in","occurred_at":timezone.now().isoformat(),"shift_id":str(self.shift.pk),"latitude":str(self.site.latitude),"longitude":str(self.site.longitude),"offline":False}
        first=self.client.post(reverse("punch_api"),data=json.dumps(payload),content_type="application/json")
        second=self.client.post(reverse("punch_api"),data=json.dumps(payload),content_type="application/json")
        self.assertEqual(first.status_code,201); self.assertEqual(second.status_code,200)
        self.assertTrue(first.json()["created"]); self.assertFalse(second.json()["created"])

class DocumentSecurityTest(TestCase):
    def setUp(self):
        import tempfile
        from django.test import override_settings
        from .models import DocumentType
        self.temp=tempfile.TemporaryDirectory(); self.override=override_settings(MEDIA_ROOT=self.temp.name); self.override.enable()
        User=get_user_model(); self.user=User.objects.create_user(username="hr@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Docs LLC",display_name="Docs",slug="docs")
        Membership.objects.create(user=self.user,organization=self.org,role=Membership.Role.HR)
        self.person=Person.objects.create(organization=self.org,first_name="Morgan",last_name="Diaz")
        self.kind=DocumentType.objects.create(organization=self.org,name="Policy acknowledgment",code="policy")
        self.client.force_login(self.user)
    def tearDown(self): self.override.disable(); self.temp.cleanup()
    def test_private_document_upload_and_download_are_audited(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import PersonDocument
        upload=SimpleUploadedFile("record.pdf",b"%PDF-1.4\nvalid test record",content_type="application/pdf")
        response=self.client.post(reverse("document_upload"),{"person":self.person.pk,"document_type":self.kind.pk,"file":upload})
        self.assertRedirects(response,reverse("documents")); document=PersonDocument.objects.get()
        self.assertEqual(document.scan_status,PersonDocument.ScanStatus.CLEAN)
        download=self.client.get(reverse("document_download",args=[document.pk]))
        self.assertEqual(download.status_code,200); self.assertEqual(download["Cache-Control"],"private, no-store")
        self.assertTrue(AuditEvent.objects.filter(action="document.downloaded",target_id=str(document.pk)).exists())
    def test_eicar_upload_is_rejected_and_not_persisted(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import PersonDocument
        payload=b"%PDF-1.4\nX5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE"
        response=self.client.post(reverse("document_upload"),{"person":self.person.pk,"document_type":self.kind.pk,"file":SimpleUploadedFile("bad.pdf",payload,content_type="application/pdf")})
        self.assertEqual(response.status_code,200); self.assertFalse(PersonDocument.objects.exists())

class NotificationServiceTest(TestCase):
    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Credential, CredentialType, Notification
        User=get_user_model(); self.owner=User.objects.create_user(username="notify@example.com",email="notify@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Notify LLC",display_name="Notify",slug="notify",email_from="alerts@example.com",email_provider=Organization.EmailProvider.POSTMARK)
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        person=Person.objects.create(organization=self.org,user=self.owner,first_name="Sam",last_name="Owner")
        kind=CredentialType.objects.create(organization=self.org,name="Officer registration",code="registration",warning_days=60)
        self.credential=Credential.objects.create(organization=self.org,person=person,credential_type=kind,status=Credential.Status.ACTIVE,expires_on=timezone.localdate()+timedelta(days=30))
    def test_compliance_reminders_are_deduplicated(self):
        from .models import Notification
        from .services import queue_compliance_reminders
        self.assertEqual(queue_compliance_reminders(),2)
        self.assertEqual(queue_compliance_reminders(),0)
        self.assertEqual(Notification.objects.count(),2)
    def test_postmark_delivery_uses_configured_adapter(self):
        from unittest.mock import patch,Mock
        from django.test import override_settings
        from .models import Notification
        from .services import deliver_notification
        item=Notification.objects.create(organization=self.org,recipient=self.owner,channel=Notification.Channel.EMAIL,event_type="test",subject="Test",body="Safe body")
        response=Mock(); response.raise_for_status.return_value=None
        with override_settings(POSTMARK_SERVER_TOKEN="secret"), patch("requests.post",return_value=response) as post:
            deliver_notification(item)
        item.refresh_from_db(); self.assertEqual(item.status,Notification.Status.SENT); self.assertEqual(item.attempts,1)
        self.assertEqual(post.call_args.kwargs["headers"]["X-Postmark-Server-Token"],"secret")

class CsvImportTest(TestCase):
    def setUp(self):
        User=get_user_model(); self.user=User.objects.create_user(username="import@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Import LLC",display_name="Import",slug="import")
        Membership.objects.create(user=self.user,organization=self.org,role=Membership.Role.ADMIN); self.client.force_login(self.user)
    def test_people_import_previews_applies_and_is_idempotent(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import ImportBatch
        payload=b"first_name,last_name,email,status,is_unarmed_officer\nAvery,Nguyen,avery@example.com,active,true\n"
        response=self.client.post(reverse("imports"),{"entity":"people","file":SimpleUploadedFile("people.csv",payload,content_type="text/csv")})
        self.assertEqual(response.status_code,200); batch=ImportBatch.objects.get(); self.assertEqual(batch.status,ImportBatch.Status.PREVIEW)
        apply_response=self.client.post(reverse("import_apply",args=[batch.pk])); self.assertRedirects(apply_response,reverse("imports"))
        self.assertTrue(self.org.people.filter(email="avery@example.com",is_unarmed_officer=True).exists())
        self.client.post(reverse("import_apply",args=[batch.pk])); self.assertEqual(self.org.people.filter(email="avery@example.com").count(),1)
    def test_invalid_import_reports_row_errors_without_changes(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import ImportBatch
        response=self.client.post(reverse("imports"),{"entity":"people","file":SimpleUploadedFile("people.csv",b"first_name,last_name,email\nMissing,Email,\n",content_type="text/csv")})
        self.assertEqual(response.status_code,200); self.assertEqual(ImportBatch.objects.get().status,ImportBatch.Status.INVALID); self.assertFalse(self.org.people.exists())

class PayrollWorkflowTest(TestCase):
    def setUp(self):
        import uuid
        from datetime import timedelta
        from django.utils import timezone
        User=get_user_model(); self.manager=User.objects.create_user(username="payroll@example.com",password="correct horse battery staple")
        self.officer_user=User.objects.create_user(username="worker@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Payroll LLC",display_name="Payroll",slug="payroll")
        Membership.objects.create(user=self.manager,organization=self.org,role=Membership.Role.PAYROLL); Membership.objects.create(user=self.officer_user,organization=self.org,role=Membership.Role.OFFICER)
        self.person=Person.objects.create(organization=self.org,user=self.officer_user,first_name="Jamie",last_name="Fox")
        self.start=timezone.now().replace(hour=0,minute=0,second=0,microsecond=0); self.end=self.start+timedelta(days=7)
        self.punch_in=Punch.objects.create(organization=self.org,person=self.person,client_event_id=uuid.uuid4(),kind=Punch.Kind.IN,occurred_at=self.start+timedelta(hours=8))
        Punch.objects.create(organization=self.org,person=self.person,client_event_id=uuid.uuid4(),kind=Punch.Kind.OUT,occurred_at=self.start+timedelta(hours=16))
    def test_correction_approval_changes_payroll_without_mutating_raw_punch(self):
        from datetime import timedelta
        original=self.punch_in.occurred_at
        item=PunchAdjustment.objects.create(organization=self.org,punch=self.punch_in,requested_by=self.officer_user,proposed_at=original+timedelta(minutes=30),reason="Forgot to clock in after briefing")
        self.client.force_login(self.manager); response=self.client.post(reverse("adjustment_review",args=[item.pk]),{"action":"approved","note":"Supervisor confirmed"})
        self.assertRedirects(response,reverse("time_review")); self.punch_in.refresh_from_db(); item.refresh_from_db()
        self.assertEqual(self.punch_in.occurred_at,original); self.assertEqual(item.status,PunchAdjustment.Status.APPROVED)
    def test_payroll_approval_locks_period_and_exports_snapshot(self):
        from .services import create_payroll_run
        run=create_payroll_run(organization=self.org,start=self.start,end=self.end,actor=self.manager)
        self.client.force_login(self.manager); response=self.client.post(reverse("payroll_approve",args=[run.pk])); self.assertRedirects(response,reverse("payroll"));run.refresh_from_db();self.assertEqual(run.status,PayrollRun.Status.APPROVED)
        export=self.client.get(reverse("payroll_run_export",args=[run.pk]));self.assertEqual(export.status_code,200);self.assertIn("Jamie Fox",export.content.decode())
    def test_pending_punch_blocks_payroll_approval(self):
        self.punch_in.review_status=Punch.Review.PENDING;self.punch_in.save(update_fields=["review_status"])
        from .services import create_payroll_run
        run=create_payroll_run(organization=self.org,start=self.start,end=self.end,actor=self.manager)
        self.client.force_login(self.manager);self.client.post(reverse("payroll_approve",args=[run.pk]));run.refresh_from_db();self.assertEqual(run.status,PayrollRun.Status.DRAFT)

class RecordDispositionTest(TestCase):
    def setUp(self):
        import tempfile
        from django.test import override_settings
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import DocumentType
        from .services import store_person_document
        self.temp=tempfile.TemporaryDirectory();self.override=override_settings(MEDIA_ROOT=self.temp.name);self.override.enable()
        User=get_user_model();self.owner=User.objects.create_user(username="records@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Records LLC",display_name="Records",slug="records")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        person=Person.objects.create(organization=self.org,first_name="Records",last_name="Worker");kind=DocumentType.objects.create(organization=self.org,name="Record",code="record",retention_days=1)
        self.document=store_person_document(organization=self.org,person=person,document_type=kind,upload=SimpleUploadedFile("record.pdf",b"%PDF-1.4\nrecord",content_type="application/pdf"),actor=self.owner)
        self.client.force_login(self.owner)
    def tearDown(self):self.override.disable();self.temp.cleanup()
    def test_legal_hold_blocks_disposition(self):
        from .models import DispositionRequest
        item=DispositionRequest.objects.create(organization=self.org,document=self.document,action=DispositionRequest.Action.DELETE,reason="Retention period completed",requested_by=self.owner)
        self.document.legal_hold=True;self.document.save(update_fields=["legal_hold"])
        response=self.client.post(reverse("disposition_execute",args=[item.pk]));self.assertRedirects(response,reverse("retention_review"));item.refresh_from_db();self.assertEqual(item.status,DispositionRequest.Status.PENDING)
    def test_delete_removes_file_but_keeps_tombstone_metadata(self):
        from .models import DispositionRequest
        item=DispositionRequest.objects.create(organization=self.org,document=self.document,action=DispositionRequest.Action.DELETE,reason="Approved retention disposition",requested_by=self.owner)
        self.client.post(reverse("disposition_execute",args=[item.pk]));self.document.refresh_from_db();item.refresh_from_db()
        self.assertIsNotNone(self.document.deleted_at);self.assertFalse(self.document.file);self.assertEqual(item.status,DispositionRequest.Status.EXECUTED)
        event=AuditEvent.objects.get(action="document.deleted");self.assertEqual(event.metadata["sha256"],self.document.sha256)

class SsoMfaSecurityTest(TestCase):
    def setUp(self):
        User=get_user_model();self.user=User.objects.create_user(username="secure@example.com",email="secure@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Secure LLC",display_name="Secure",slug="secure",mfa_required_roles=[Membership.Role.ADMIN])
        Membership.objects.create(user=self.user,organization=self.org,role=Membership.Role.ADMIN);self.client.force_login(self.user)
    def test_required_role_is_redirected_to_totp_enrollment(self):
        response=self.client.get(reverse("dashboard"));self.assertRedirects(response,reverse("mfa_activate_totp"),fetch_redirect_response=False)
    def test_sso_provider_routes_are_registered(self):
        self.assertIn("/accounts/google/login/",reverse("google_login"));self.assertIn("/accounts/microsoft/login/",reverse("microsoft_login"))
    def test_owner_can_configure_mfa_roles_after_enrollment_bypass(self):
        self.org.mfa_required_roles=[];self.org.save(update_fields=["mfa_required_roles"])
        response=self.client.post(reverse("security_settings"),{"mfa_required_roles":[Membership.Role.ADMIN,Membership.Role.OWNER]})
        self.assertRedirects(response,reverse("security_settings"),fetch_redirect_response=False);self.org.refresh_from_db();self.assertIn(Membership.Role.OWNER,self.org.mfa_required_roles)
        self.assertTrue(AuditEvent.objects.filter(action="security.mfa_policy_updated").exists())

class ProductionReadinessTest(TestCase):
    def test_readiness_checks_database_and_cache(self):
        response=self.client.get(reverse("ready"));self.assertEqual(response.status_code,200);self.assertEqual(response.json()["status"],"ready")
    def test_theme_css_is_authenticated_and_uses_validated_colors(self):
        User=get_user_model();user=User.objects.create_user(username="theme@example.com",password="correct horse battery staple")
        org=Organization.objects.create(legal_name="Theme LLC",display_name="Theme",slug="theme",primary_color="#112233",accent_color="#AABBCC")
        Membership.objects.create(user=user,organization=org,role=Membership.Role.OFFICER)
        self.assertEqual(self.client.get(reverse("theme_css")).status_code,302)
        self.client.force_login(user);response=self.client.get(reverse("theme_css"));self.assertContains(response,"--brand:#112233");self.assertEqual(response["Content-Type"],"text/css")
        self.assertNotIn("'unsafe-inline'",self.client.get(reverse("health")).headers["Content-Security-Policy"])

class MultiTenantIsolationTest(TestCase):
    def setUp(self):
        User=get_user_model();self.user=User.objects.create_user(username="multi@example.com",password="correct horse battery staple")
        self.first=Organization.objects.create(legal_name="First LLC",display_name="First",slug="first")
        self.second=Organization.objects.create(legal_name="Second LLC",display_name="Second",slug="second")
        Membership.objects.create(user=self.user,organization=self.first,role=Membership.Role.ADMIN)
        Membership.objects.create(user=self.user,organization=self.second,role=Membership.Role.ADMIN)
        Person.objects.create(organization=self.first,first_name="First",last_name="Only")
        Person.objects.create(organization=self.second,first_name="Second",last_name="Only")
        self.client.force_login(self.user)
    def test_explicit_tenant_selection_scopes_following_requests(self):
        response=self.client.post(reverse("tenant_select"),{"organization_id":self.second.pk})
        self.assertRedirects(response,reverse("dashboard"));directory=self.client.get(reverse("people"))
        self.assertContains(directory,"Second Only");self.assertNotContains(directory,"First Only")
    def test_verified_host_overrides_stale_session_selection(self):
        from .models import OrganizationDomain
        session=self.client.session;session["active_organization_id"]=str(self.first.pk);session.save()
        OrganizationDomain.objects.create(organization=self.second,hostname="portal.second.example",verified=True,status=OrganizationDomain.Status.VERIFIED)
        response=self.client.get(reverse("people"),HTTP_HOST="portal.second.example")
        self.assertContains(response,"Second Only");self.assertNotContains(response,"First Only")
    def test_unknown_host_is_rejected_before_tenant_resolution(self):
        response=self.client.get(reverse("dashboard"),HTTP_HOST="attacker.example")
        self.assertEqual(response.status_code,400)
    def test_cross_tenant_form_choices_are_not_exposed(self):
        response=self.client.post(reverse("tenant_select"),{"organization_id":self.first.pk});self.assertEqual(response.status_code,302)
        page=self.client.get(reverse("credential_create"));self.assertNotContains(page,"Second Only")
    def test_nonmember_cannot_select_tenant(self):
        third=Organization.objects.create(legal_name="Third LLC",display_name="Third",slug="third")
        self.assertEqual(self.client.post(reverse("tenant_select"),{"organization_id":third.pk}).status_code,404)
