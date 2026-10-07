import json
import uuid
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from .models import AuditEvent, AuditRedaction, AuditSeal, AuthorityScope, AvailabilityRule, Branch, Checkpoint, Client, ClockKiosk, ComplianceRule, Credential, CredentialRegistryCheck, CredentialType, CustomFieldDefinition, DeliveryEvent, DispositionRequest, DocumentType, HoldOver, ImportBatch, Membership, MembershipInvitation, MessageConsent, Notification, Suppression, OfflineClockDevice, OnboardingItem, OnboardingTask, Organization, OrganizationDomain, PayCategory, PayCode, PayrollLockSegment, PayrollRun, Person, PersonCustomValue, PersonDocument, Punch, PunchAdjustment, ShiftHourDesignation, ReportSnapshot, RuleRevision, Shift, ShiftClaim, ShiftExchange, ShiftSwap, ShiftTemplate, Site, TimeOffRequest, TimePolicy, TimePolicyOverride, TrainingRecord

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

    def test_login_without_next_redirects_to_dashboard(self):
        response=self.client.post(reverse("account_login"),{"login":"owner@example.com","password":"correct horse battery staple"})
        self.assertRedirects(response,reverse("dashboard"))

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

class FirstVerticalSliceTest(TestCase):
    def setUp(self):
        User=get_user_model()
        self.owner=User.objects.create_user(username="owner@example.com",email="owner@example.com",password="correct horse battery staple")
        self.officer=User.objects.create_user(username="officer@example.com",email="officer@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Pilot Security LLC",display_name="Pilot Security",slug="pilot-security")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.officer,organization=self.org,role=Membership.Role.OFFICER)

    def test_owner_can_invite_and_new_admin_can_accept_once(self):
        self.client.force_login(self.owner)
        response=self.client.post(reverse("team"),{"email":"New.Admin@Example.com","role":Membership.Role.ADMIN},follow=True)
        self.assertEqual(response.status_code,200)
        invitation=MembershipInvitation.objects.get()
        self.assertEqual(invitation.email,"new.admin@example.com")
        self.assertContains(response,"Invitation ready")
        invitation_url=response.context["invitation_url"]
        token=invitation_url.rstrip("/").rsplit("/",1)[-1]
        self.assertNotEqual(invitation.token_hash,token)
        notice=Notification.objects.get(event_type="membership.invitation")
        self.assertEqual(notice.destination,"new.admin@example.com")
        self.assertIn(token,notice.body)
        self.client.logout()
        accepted=self.client.post(reverse("invitation_accept",args=[token]),{"first_name":"New","last_name":"Admin","password":"long secure password","password_confirmation":"long secure password"})
        self.assertRedirects(accepted,reverse("dashboard"))
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)
        membership=Membership.objects.get(organization=self.org,user__email="new.admin@example.com")
        self.assertEqual(membership.role,Membership.Role.ADMIN)
        self.assertTrue(AuditEvent.objects.filter(organization=self.org,action="membership.invited").exists())
        self.assertTrue(AuditEvent.objects.filter(organization=self.org,action="membership.invitation_accepted").exists())
        self.assertEqual(self.client.get(reverse("invitation_accept",args=[token])).status_code,410)

    def test_officer_cannot_manage_team_access(self):
        self.client.force_login(self.officer)
        self.assertEqual(self.client.get(reverse("team")).status_code,403)

    def test_authenticated_shell_has_accessibility_landmarks(self):
        self.client.force_login(self.owner)
        response=self.client.get(reverse("dashboard"))
        self.assertContains(response,'href="#main-content"')
        self.assertContains(response,'<main id="main-content"')
        self.assertContains(response,'aria-label="Main navigation"')

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
        from django.db import DatabaseError, connection
        from .services import verify_audit_chain
        event=AuditEvent.objects.create(organization=self.org,actor=self.user,action="created",target_type="person",target_id=str(self.person.pk),metadata={"safe":"yes"})
        self.assertEqual(verify_audit_chain(self.org),[])
        tamper='UPDATE core_auditevent SET metadata=%s WHERE id=%s'
        arguments=['{\"safe\":\"no\"}',event.pk.hex]
        if connection.vendor=="mysql":
            # The database itself must refuse the write (migration 0018). sqlite has no
            # equivalent, so this leg only runs where the guarantee is actually installed.
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema=DATABASE() AND event_object_table='core_auditevent' AND action_timing='BEFORE'")
                self.assertEqual(cursor.fetchone()[0],2,"BEFORE UPDATE and BEFORE DELETE guards are missing")
            with self.assertRaises(DatabaseError):
                with connection.cursor() as cursor: cursor.execute(tamper,arguments)
            self.assertEqual(verify_audit_chain(self.org),[])
            return
        with connection.cursor() as cursor:cursor.execute(tamper,arguments)
        self.assertEqual(verify_audit_chain(self.org),[str(event.pk)])

    def test_events_written_in_the_same_clock_tick_still_verify(self):
        """A frozen clock forces every write onto one timestamp; ties used to be walked in uuid order."""
        from unittest import mock
        from django.utils import timezone
        from .services import verify_audit_chain
        frozen=timezone.now()
        with mock.patch("core.models.timezone.now",return_value=frozen):
            for index in range(12):
                AuditEvent.objects.create(organization=self.org,actor=self.user,action="tick",target_type="person",target_id=str(index))
        ordered=list(self.org.audit_events.order_by("occurred_at","id"))
        self.assertEqual([str(index) for index in range(12)],[event.target_id for event in ordered])
        self.assertEqual(12,len({event.occurred_at for event in ordered}))
        self.assertEqual([],verify_audit_chain(self.org))

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
        for key in ("SECRET_KEY", "DEBUG", "ALLOWED_HOSTS", "REDIS_URL"):
            env.pop(key, None)
        # A developer .env must not be able to supply the secret this test expects to be absent.
        env["DOTENV_PATH"] = str(__import__("pathlib").Path(__file__).resolve().parent / "absent-env-file")
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

    def test_proxy_ssl_header_trusted_only_when_configured(self):
        import os
        import pathlib
        import subprocess
        import sys

        def resolve(header_value):
            env = os.environ.copy()
            # Explicit assignment (even empty) beats a developer .env via load_dotenv.
            env["DOTENV_PATH"] = str(pathlib.Path(__file__).resolve().parent / "absent-env-file")
            env["DEBUG"] = "true"
            env["TRUST_PROXY_SSL_HEADER"] = header_value
            result = subprocess.run(
                [sys.executable, "-c", "import config.settings as s; print(s.SECURE_PROXY_SSL_HEADER)"],
                cwd=str(pathlib.Path(__file__).resolve().parent.parent),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        self.assertEqual(resolve(""), "None")
        self.assertEqual(resolve("false"), "None")
        self.assertEqual(resolve("true"), "('HTTP_X_FORWARDED_PROTO', 'https')")

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

    def test_enforcement_respects_schedule_and_clock_controls(self):
        from .services import shift_eligibility
        self.credential_type.blocks_scheduling=False;self.credential_type.blocks_clock_in=True;self.credential_type.save(update_fields=["blocks_scheduling","blocks_clock_in"])
        self.assertTrue(shift_eligibility(self.shift,purpose="schedule")[0])
        self.assertFalse(shift_eligibility(self.shift,purpose="clock")[0])

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
        # ignore_cleanup_errors: a streamed FileResponse keeps its OS handle open until the
        # connection's transaction state allows closing it, which on Windows can outlive the
        # directory. Forcing a close() inside the test would poison that transaction on MySQL
        # (request_finished closes the connection), so cleanup tolerance is the safe fix.
        self.temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.override=override_settings(MEDIA_ROOT=self.temp.name); self.override.enable()
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
    def test_templates_and_error_reports_are_downloadable(self):
        template=self.client.get(reverse("import_template",args=["people"]));self.assertEqual(template.status_code,200);self.assertIn("first_name",template.content.decode())
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.post(reverse("imports"),{"entity":"people","file":SimpleUploadedFile("people.csv",b"first_name,last_name,email\nMissing,Email,\n",content_type="text/csv")})
        batch=ImportBatch.objects.get();errors=self.client.get(reverse("import_errors",args=[batch.pk]));self.assertEqual(errors.status_code,200);self.assertIn("email is required",errors.content.decode())

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
        self.assertRedirects(response,reverse("time_review")+"?punches=pending&adjustments=pending"); self.punch_in.refresh_from_db(); item.refresh_from_db()
        self.assertEqual(self.punch_in.occurred_at,original); self.assertEqual(item.status,PunchAdjustment.Status.APPROVED)
    def test_payroll_approval_locks_period_and_exports_snapshot(self):
        from .services import create_payroll_run
        run=create_payroll_run(organization=self.org,start=self.start,end=self.end,actor=self.manager)
        self.client.force_login(self.manager); response=self.client.post(reverse("payroll_approve",args=[run.pk])); self.assertRedirects(response,reverse("payroll")+f"?run={run.pk}");run.refresh_from_db();self.assertEqual(run.status,PayrollRun.Status.APPROVED)
        export=self.client.get(reverse("payroll_run_export",args=[run.pk]));self.assertEqual(export.status_code,200);self.assertIn("Jamie Fox",export.content.decode())
        xlsx=self.client.get(reverse("payroll_run_export",args=[run.pk])+"?format=xlsx");self.assertEqual(xlsx.status_code,200);self.assertTrue(xlsx.content.startswith(b"PK"))
        pdf=self.client.get(reverse("payroll_run_export",args=[run.pk])+"?format=pdf");self.assertEqual(pdf.status_code,200);self.assertTrue(pdf.content.startswith(b"%PDF-1.4"))
    def test_pending_punch_blocks_payroll_approval(self):
        self.punch_in.review_status=Punch.Review.PENDING;self.punch_in.save(update_fields=["review_status"])
        from .services import create_payroll_run
        run=create_payroll_run(organization=self.org,start=self.start,end=self.end,actor=self.manager)
        self.client.force_login(self.manager);self.client.post(reverse("payroll_approve",args=[run.pk]));run.refresh_from_db();self.assertEqual(run.status,PayrollRun.Status.DRAFT)

    def lock(self):
        from .services import create_payroll_run
        run=create_payroll_run(organization=self.org,start=self.start,end=self.end,actor=self.manager)
        self.client.force_login(self.manager)
        self.client.post(reverse("payroll_approve",args=[run.pk]))
        run.refresh_from_db()
        self.assertEqual(run.status,PayrollRun.Status.APPROVED)
        return run

    def owner(self):
        User=get_user_model()
        owner=User.objects.create_user(username="payroll-owner@example.com",password="correct horse battery staple")
        Membership.objects.create(user=owner,organization=self.org,role=Membership.Role.OWNER)
        return owner

    def allow_reopening(self):
        # The payroll path itself creates the one policy row per company, so the switch is set
        # rather than inserted.
        policy,_=TimePolicy.objects.get_or_create(organization=self.org)
        policy.allow_reopen=True
        policy.save(update_fields=["allow_reopen"])

    def test_a_locked_period_stays_locked_unless_the_policy_allows_reopening(self):
        run=self.lock()
        owner=self.owner()
        self.client.force_login(owner)
        refused=self.client.post(reverse("payroll_reopen",args=[run.pk]),{"reason":"Client disputed the Tuesday hours"},follow=True)
        run.refresh_from_db()
        self.assertEqual(run.status,PayrollRun.Status.APPROVED)
        self.assertIn("not enabled",str([str(item) for item in refused.context["messages"]]))
        # The payroll approver who locked it is not the role that unlocks it, policy or not.
        self.allow_reopening()
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(reverse("payroll_reopen",args=[run.pk]),{"reason":"Client disputed the Tuesday hours"}).status_code,403)

    def test_reopening_needs_a_reason_and_unlocks_the_period_for_correction(self):
        from datetime import timedelta
        from .models import PunchAdjustment
        run=self.lock()
        self.allow_reopening()
        owner=self.owner()
        self.client.force_login(owner)
        thin=self.client.post(reverse("payroll_reopen",args=[run.pk]),{"reason":"too short"},follow=True)
        run.refresh_from_db()
        self.assertEqual(run.status,PayrollRun.Status.APPROVED)
        self.assertIn("at least 10 characters",str([str(item) for item in thin.context["messages"]]))
        adjustment=PunchAdjustment.objects.create(organization=self.org,punch=self.punch_in,requested_by=self.officer_user,
            proposed_at=self.punch_in.occurred_at+timedelta(minutes=30),reason="Forgot to clock in after briefing")
        self.client.force_login(self.manager)
        blocked=self.client.post(reverse("adjustment_review",args=[adjustment.pk]),{"action":"approved","note":""},follow=True)
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status,PunchAdjustment.Status.REQUESTED)   # the lock holds while approved
        self.assertIn("locked",str([str(item) for item in blocked.context["messages"]]).lower())
        self.client.force_login(owner)
        response=self.client.post(reverse("payroll_reopen",args=[run.pk]),{"reason":"Client disputed the Tuesday hours after the briefing was moved"},follow=True)
        self.assertRedirects(response,reverse("payroll")+f"?run={run.pk}")
        run.refresh_from_db()
        self.assertEqual(run.status,PayrollRun.Status.DRAFT)
        self.assertEqual((run.reopen_count,run.reopened_by_id),(1,owner.pk))
        self.assertIn("disputed",run.reopen_reason)
        self.assertIsNone(run.approved_by_id)   # a draft must not present an approver who no longer stands behind it
        self.assertTrue(AuditEvent.objects.filter(action="payroll.reopened",target_type="payroll_run").exists())
        self.client.force_login(self.manager)
        self.client.post(reverse("adjustment_review",args=[adjustment.pk]),{"action":"approved","note":"Supervisor confirmed"})
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status,PunchAdjustment.Status.APPROVED)

    def test_a_reopened_period_must_be_regenerated_before_it_can_be_locked_again(self):
        from .services import create_payroll_run
        run=self.lock()
        self.allow_reopening()
        owner=self.owner()
        self.client.force_login(owner)
        self.client.post(reverse("payroll_reopen",args=[run.pk]),{"reason":"Recount the armed patrol differential for week 4"})
        run.refresh_from_db()
        self.assertEqual(run.status,PayrollRun.Status.DRAFT)
        self.assertTrue(run.exceptions)
        self.client.force_login(self.manager)
        self.client.post(reverse("payroll_approve",args=[run.pk]))
        run.refresh_from_db()
        self.assertEqual(run.status,PayrollRun.Status.DRAFT)   # the stale numbers are not re-lockable
        regenerate=create_payroll_run(organization=self.org,start=self.start,end=self.end,actor=self.manager)
        self.assertEqual(regenerate.pk,run.pk)
        regenerate.refresh_from_db()
        self.assertEqual(regenerate.exceptions,[])
        self.client.post(reverse("payroll_approve",args=[run.pk]))
        regenerate.refresh_from_db()
        self.assertEqual(regenerate.status,PayrollRun.Status.APPROVED)
        self.client.force_login(owner)
        self.client.post(reverse("payroll_reopen",args=[run.pk]),{"reason":"Second dispute raised by the client's site manager"})
        regenerate.refresh_from_db()
        self.assertEqual(regenerate.reopen_count,2)   # the habit is counted, not hidden

class RecordDispositionTest(TestCase):
    def setUp(self):
        import tempfile
        from django.test import override_settings
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import DocumentType
        from .services import store_person_document
        self.temp=tempfile.TemporaryDirectory();self.override=override_settings(MEDIA_ROOT=self.temp.name);self.override.enable()
        User=get_user_model();self.owner=User.objects.create_user(username="records@example.com",password="correct horse battery staple")
        self.second_owner=User.objects.create_user(username="records2@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Records LLC",display_name="Records",slug="records")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.second_owner,organization=self.org,role=Membership.Role.OWNER)
        person=Person.objects.create(organization=self.org,first_name="Records",last_name="Worker");kind=DocumentType.objects.create(organization=self.org,name="Record",code="record",retention_days=1)
        self.document=store_person_document(organization=self.org,person=person,document_type=kind,upload=SimpleUploadedFile("record.pdf",b"%PDF-1.4\nrecord",content_type="application/pdf"),actor=self.owner)
        self.client.force_login(self.owner)
    def tearDown(self):self.override.disable();self.temp.cleanup()
    def test_legal_hold_blocks_disposition(self):
        from .models import DispositionRequest
        item=DispositionRequest.objects.create(organization=self.org,document=self.document,action=DispositionRequest.Action.DELETE,reason="Retention period completed",requested_by=self.owner)
        self.document.legal_hold=True;self.document.save(update_fields=["legal_hold"])
        response=self.client.post(reverse("disposition_execute",args=[item.pk]));self.assertRedirects(response,reverse("retention_review"));item.refresh_from_db();self.assertEqual(item.status,DispositionRequest.Status.PENDING)
    def test_requester_cannot_authorize_their_own_disposition(self):
        from .models import DispositionRequest
        item=DispositionRequest.objects.create(organization=self.org,document=self.document,action=DispositionRequest.Action.DELETE,reason="Approved retention disposition",requested_by=self.owner)
        self.client.post(reverse("disposition_execute",args=[item.pk]));item.refresh_from_db();self.document.refresh_from_db()
        self.assertEqual(item.status,DispositionRequest.Status.PENDING);self.assertIsNone(item.approved_by);self.assertIsNone(self.document.deleted_at)
        self.assertTrue(self.document.file.name)
        from django.core.files.storage import default_storage
        self.assertTrue(default_storage.exists(self.document.file.name))
    def test_delete_removes_file_but_keeps_tombstone_metadata(self):
        from .models import DispositionRequest
        item=DispositionRequest.objects.create(organization=self.org,document=self.document,action=DispositionRequest.Action.DELETE,reason="Approved retention disposition",requested_by=self.owner)
        self.client.force_login(self.second_owner)
        self.client.post(reverse("disposition_execute",args=[item.pk]));self.document.refresh_from_db();item.refresh_from_db()
        self.assertIsNotNone(self.document.deleted_at);self.assertFalse(self.document.file);self.assertEqual(item.status,DispositionRequest.Status.EXECUTED)
        self.assertEqual(item.approved_by_id,self.second_owner.pk)
        event=AuditEvent.objects.get(action="document.deleted");self.assertEqual(event.metadata["sha256"],self.document.sha256)

class SsoMfaSecurityTest(TestCase):
    def setUp(self):
        User=get_user_model();self.user=User.objects.create_user(username="secure@example.com",email="secure@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Secure LLC",display_name="Secure",slug="secure",mfa_required_roles=[Membership.Role.ADMIN])
        Membership.objects.create(user=self.user,organization=self.org,role=Membership.Role.ADMIN);self.client.force_login(self.user)
    def test_required_role_is_redirected_to_totp_enrollment(self):
        response=self.client.get(reverse("dashboard"));self.assertRedirects(response,reverse("mfa_activate_totp"),fetch_redirect_response=False)
    def test_reauthentication_page_is_not_redirected_back_to_enrollment(self):
        response=self.client.get(reverse("account_reauthenticate"))
        self.assertEqual(response.status_code,200)
    def test_sso_provider_routes_are_registered(self):
        self.assertIn("/accounts/google/login/",reverse("google_login"));self.assertIn("/accounts/microsoft/login/",reverse("microsoft_login"))
    def test_owner_can_configure_mfa_roles_after_enrollment_bypass(self):
        self.org.mfa_required_roles=[];self.org.save(update_fields=["mfa_required_roles"])
        response=self.client.post(reverse("security_settings"),{"mfa_required_roles":[Membership.Role.ADMIN,Membership.Role.OWNER]})
        self.assertRedirects(response,reverse("security_settings"),fetch_redirect_response=False);self.org.refresh_from_db();self.assertIn(Membership.Role.OWNER,self.org.mfa_required_roles)
        self.assertTrue(AuditEvent.objects.filter(action="security.mfa_policy_updated").exists())

class MfaEnrollmentResourcesTest(TestCase):
    def setUp(self):
        from allauth.account.models import EmailAddress
        from django.core.files.base import ContentFile
        from django.test import override_settings

        storage = override_settings(STORAGES={
            "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        })
        storage.enable()
        self.addCleanup(storage.disable)
        self.user = get_user_model().objects.create_user(
            username="enrollment@example.com", email="enrollment@example.com",
            password="correct horse battery staple",
        )
        EmailAddress.objects.create(user=self.user, email=self.user.email, verified=True, primary=True)
        self.org = Organization.objects.create(
            legal_name="Enrollment LLC", display_name="Enrollment", slug="enrollment",
            mfa_required_roles=[Membership.Role.OWNER],
        )
        Membership.objects.create(user=self.user, organization=self.org, role=Membership.Role.OWNER)
        self.org.logo.save("logo.svg", ContentFile(b'<svg xmlns="http://www.w3.org/2000/svg"/>'))
        self.client.post(reverse("account_login"), {
            "login": self.user.email, "password": "correct horse battery staple",
        })

    def load_enrollment_resources(self):
        from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY

        page = self.client.get(reverse("mfa_activate_totp"))
        self.assertEqual(page.status_code, 200)
        secret = self.client.session[SECRET_SESSION_KEY]
        self.assertContains(page, secret)
        resources = (
            ("theme_css", "text/css"),
            ("brand_logo", "image/svg+xml"),
            ("manifest", "application/json"),
            ("service_worker", "application/javascript"),
        )
        for name, content_type in resources:
            with self.subTest(resource=name):
                response = self.client.get(reverse(name), follow=True)
                if getattr(response, "file_to_stream", None) is not None:
                    self.addCleanup(response.file_to_stream.close)
                self.assertEqual(response.redirect_chain, [])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], content_type)
                self.assertEqual(self.client.session[SECRET_SESSION_KEY], secret)
        return secret

    def test_original_qr_code_enrolls_after_browser_resources_load(self):
        import time
        from allauth.mfa import app_settings
        from allauth.mfa.models import Authenticator
        from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY, format_hotp_value, hotp_value
        from allauth.mfa.utils import decrypt
        from unittest.mock import patch

        secret = self.load_enrollment_resources()
        now = time.time()
        code = format_hotp_value(hotp_value(secret, int(now) // app_settings.TOTP_PERIOD))
        with patch("allauth.mfa.totp.internal.auth.time.time", return_value=now):
            response = self.client.post(reverse("mfa_activate_totp"), {"code": code})
        self.assertRedirects(response, reverse("mfa_view_recovery_codes"), fetch_redirect_response=False)
        authenticator = Authenticator.objects.get(user=self.user, type=Authenticator.Type.TOTP)
        self.assertEqual(decrypt(authenticator.data["secret"]), secret)
        self.assertNotIn(SECRET_SESSION_KEY, self.client.session)

    def test_invalid_code_and_resource_reload_preserve_displayed_secret(self):
        from allauth.mfa.models import Authenticator
        from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY

        secret = self.load_enrollment_resources()
        response = self.client.post(reverse("mfa_activate_totp"), {"code": "invalid"})
        self.assertContains(response, "Incorrect code")
        self.assertContains(response, secret)
        for name in ("theme_css", "brand_logo", "manifest", "service_worker"):
            resource = self.client.get(reverse(name), follow=True)
            if getattr(resource, "file_to_stream", None) is not None:
                self.addCleanup(resource.file_to_stream.close)
        self.assertEqual(self.client.session[SECRET_SESSION_KEY], secret)
        self.assertFalse(Authenticator.objects.filter(user=self.user).exists())

    def test_business_access_remains_gated(self):
        for method in (self.client.get, self.client.post):
            for name in ("dashboard", "people", "clock", "security_settings"):
                with self.subTest(method=method.__name__, route=name):
                    response = method(reverse(name))
                    self.assertRedirects(response, reverse("mfa_activate_totp"), fetch_redirect_response=False)

    def test_resource_exemptions_are_exact_paths(self):
        for name in ("theme_css", "brand_logo", "manifest", "service_worker"):
            with self.subTest(resource=name):
                response = self.client.get(reverse(name) + "/not-a-resource")
                self.assertRedirects(response, reverse("mfa_activate_totp"), fetch_redirect_response=False)

    def test_resource_exemptions_only_allow_reads(self):
        for name in ("theme_css", "brand_logo", "manifest", "service_worker"):
            with self.subTest(resource=name):
                self.assertEqual(self.client.head(reverse(name)).status_code, 200)
                response = self.client.post(reverse(name))
                self.assertRedirects(response, reverse("mfa_activate_totp"), fetch_redirect_response=False)

    def test_resource_exemptions_do_not_bypass_authentication(self):
        self.client.logout()
        for name in ("theme_css", "brand_logo"):
            with self.subTest(resource=name):
                response = self.client.get(reverse(name))
                self.assertRedirects(
                    response, reverse("account_login") + "?next=" + reverse(name),
                    fetch_redirect_response=False,
                )

    def test_worker_precache_does_not_follow_business_redirects(self):
        from django.test import RequestFactory
        from .views import service_worker

        body = service_worker(RequestFactory().get(reverse("service_worker"))).content.decode()
        self.assertIn('fetch(path,{redirect:"error"})', body)
        self.assertIn('fetch(path,{credentials:"same-origin",redirect:"error"})', body)


class AllauthThemingTest(TestCase):
    """allauth ships a bare default layout, and RequiredMfaMiddleware sends owners straight
    into those enrollment pages. They must render inside the product chrome, not allauth's."""

    def setUp(self):
        User=get_user_model();self.user=User.objects.create_user(username="themed@example.com",email="themed@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Theme LLC",display_name="Theme",slug="theme")
        Membership.objects.create(user=self.user,organization=self.org,role=Membership.Role.OWNER)

    def test_mfa_enrollment_renders_inside_the_app_shell(self):
        # allauth stamps "recently authenticated" on a real login; force_login would skip
        # that and the enrollment view would bounce to reauthentication instead.
        self.client.post(reverse("account_login"),{"login":"themed@example.com","password":"correct horse battery staple"})
        response=self.client.get(reverse("mfa_activate_totp"))
        self.assertContains(response,"css/app.css")
        self.assertContains(response,'aria-label="Main navigation"')
        self.assertContains(response,"Sign out")
        self.assertContains(response,"Activate Authenticator App")
        self.assertContains(response,"qr-img")

    def test_anonymous_allauth_pages_are_branded(self):
        response=self.client.get(reverse("account_reset_password"))
        self.assertContains(response,"css/app.css")
        self.assertContains(response,"allauth-page entrance")
        self.assertContains(response,"Password Reset")

    def test_login_page_keeps_the_custom_split_layout(self):
        response=self.client.get(reverse("account_login"))
        self.assertContains(response,"login-shell")
        self.assertNotContains(response,"allauth-page")

    def test_mfa_index_and_reauthenticate_render_in_the_shell(self):
        self.client.force_login(self.user)
        index=self.client.get(reverse("mfa_index"))
        self.assertContains(index,"allauth-panel")
        self.assertContains(index,"Activate")
        reauth=self.client.get(reverse("account_reauthenticate"))
        self.assertContains(reauth,"css/app.css")
        self.assertContains(reauth,"Confirm Access")

class ProductionReadinessTest(TestCase):
    def test_health_probes_answer_for_the_container_address(self):
        from django.test import override_settings
        # Compose and App Platform probes present 127.0.0.1 or the pod address, which is never
        # a tenant hostname; only the readiness routes may bypass the host allowlist.
        with override_settings(PLATFORM_HOSTS=["portal.example.com"]):
            self.assertEqual(self.client.get(reverse("health"), HTTP_HOST="127.0.0.1").status_code, 200)
            self.assertEqual(self.client.get(reverse("ready"), HTTP_HOST="127.0.0.1").status_code, 200)
            self.assertEqual(self.client.get(reverse("dashboard"), HTTP_HOST="127.0.0.1").status_code, 400)
            self.assertEqual(self.client.get(reverse("health"), HTTP_HOST="attacker.example").status_code, 200)

    def test_probes_survive_the_https_redirect(self):
        from django.test import override_settings
        # SECURE_SSL_REDIRECT is off under IS_TEST by design, so production settings have to be
        # forced here: a container healthcheck probes plain HTTP and cannot follow a 301 to a
        # TLS listener it does not terminate, which left web permanently unhealthy.
        with override_settings(SECURE_SSL_REDIRECT=True, PLATFORM_HOSTS=["portal.example.com"]):
            self.assertEqual(self.client.get(reverse("health"), HTTP_HOST="127.0.0.1").status_code, 200)
            self.assertEqual(self.client.get(reverse("ready"), HTTP_HOST="127.0.0.1").status_code, 200)
            self.assertEqual(self.client.get(reverse("dashboard"), HTTP_HOST="portal.example.com").status_code, 301)

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

    def test_per_object_routes_reject_other_tenants_identifiers(self):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from .services import store_person_document
        other=Organization.objects.create(legal_name="Other LLC",display_name="Other",slug="other-objects")
        other_member=Membership.objects.create(user=self.user,organization=other,role=Membership.Role.ADMIN)
        other_person=Person.objects.create(organization=other,first_name="Other",last_name="Person")
        other_kind=DocumentType.objects.create(organization=other,name="Record",code="other-record")
        other_batch=ImportBatch.objects.create(organization=other,entity=ImportBatch.Entity.BRANCHES,source_name="b.csv",source_hash="x",rows=[],errors=[],status=ImportBatch.Status.PREVIEW,created_by=self.user)
        other_notice=Notification.objects.create(organization=other,recipient=self.user,channel=Notification.Channel.IN_APP,event_type="x",subject="x",body="x")
        other_event=AuditEvent.objects.create(organization=other,actor=self.user,action="other.action",target_type="person",target_id=str(other_person.pk))
        other_run=PayrollRun.objects.create(organization=other,period_start=__import__("django.utils.timezone",fromlist=["now"]).now(),period_end=__import__("django.utils.timezone",fromlist=["now"]).now()+__import__("datetime",fromlist=["timedelta"]).timedelta(days=1),created_by=self.user)
        self.client.force_login(self.user)
        # Select the first tenant explicitly so every request below is scoped to it.
        self.client.post(reverse("tenant_select"),{"organization_id":self.first.pk})
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as media, override_settings(MEDIA_ROOT=media):
            other_document=store_person_document(organization=other,person=other_person,document_type=other_kind,upload=SimpleUploadedFile("other.pdf",b"%PDF-1.4\nother",content_type="application/pdf"),actor=self.user)
            requests={
                "person_detail":(reverse("person_detail",args=[other_person.pk]),"get"),
                "person_edit":(reverse("person_edit",args=[other_person.pk]),"get"),
                "person_credential_create":(reverse("person_credential_create",args=[other_person.pk]),"get"),
                "person_training_create":(reverse("person_training_create",args=[other_person.pk]),"get"),
                "person_document_upload":(reverse("person_document_upload",args=[other_person.pk]),"get"),
                "compliance_person_scope":(reverse("compliance")+"?person=%s"%other_person.pk,"get"),
                "document_download":(reverse("document_download",args=[other_document.pk]),"get"),
                "import_apply":(reverse("import_apply",args=[other_batch.pk]),"post"),
                "notification_read":(reverse("notification_read",args=[other_notice.pk]),"post"),
                "audit_redact":(reverse("audit_redact",args=[other_event.pk]),"get"),
                "audit_redaction_decide":(reverse("audit_redaction_decide",args=[999999]),"post"),
                "payroll_approve":(reverse("payroll_approve",args=[other_run.pk]),"post"),
                "payroll_run_export":(reverse("payroll_run_export",args=[other_run.pk]),"get"),
                "disposition_execute":(reverse("disposition_execute",args=[DispositionRequest.objects.create(organization=other,document=other_document,action=DispositionRequest.Action.ARCHIVE,reason="Outside this tenant",requested_by=self.user).pk]),"post"),
            }
            for name,(url,method) in requests.items():
                response=getattr(self.client,method)(url)
                self.assertEqual(response.status_code,404,msg=name)
            self.document_still_present=other_document
        self.other_organization_untouched=Organization.objects.get(pk=other.pk).people.count()
        self.assertEqual(self.other_organization_untouched,1)
        self.assertTrue(PersonDocument.objects.get(pk=self.document_still_present.pk).file)


class PunchEnforcementTest(TestCase):
    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User=get_user_model(); self.user=User.objects.create_user(username="guard-enforce@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Enforce LLC",display_name="Enforce",slug="enforce")
        Membership.objects.create(user=self.user,organization=self.org,role=Membership.Role.OFFICER)
        self.person=Person.objects.create(organization=self.org,user=self.user,first_name="Kim",last_name="One",status=Person.Status.ACTIVE)
        client=Client.objects.create(organization=self.org,name="Bank")
        self.site=Site.objects.create(organization=self.org,client=client,name="Vault",address="Dallas",latitude="32.776700",longitude="-96.797000",geofence_radius_meters=200)
        self.credential_type=CredentialType.objects.create(organization=self.org,name="Commission",code="commission")
        self.credential=Credential.objects.create(organization=self.org,person=self.person,credential_type=self.credential_type,status=Credential.Status.ACTIVE,expires_on=timezone.localdate()+timedelta(days=365))
        self.shift=Shift.objects.create(organization=self.org,site=self.site,officer=self.person,starts_at=timezone.now()-timedelta(hours=1),ends_at=timezone.now()+timedelta(hours=7),status=Shift.Status.PUBLISHED)
        self.shift.required_credentials.add(self.credential_type)
        TimePolicy.objects.create(organization=self.org,require_geofence=True)
        self.client.force_login(self.user)

    def punch(self,**overrides):
        import json,uuid
        from django.utils import timezone
        payload={"client_event_id":str(uuid.uuid4()),"kind":Punch.Kind.IN,"occurred_at":timezone.now().isoformat(),"shift_id":str(self.shift.pk),"latitude":str(self.site.latitude),"longitude":str(self.site.longitude)}
        payload.update(overrides)
        return self.client.post(reverse("punch_api"),data=json.dumps(payload),content_type="application/json")

    def test_naive_timestamp_is_rejected_instead_of_guessed(self):
        from django.utils import timezone
        response=self.punch(occurred_at=timezone.now().replace(tzinfo=None).isoformat())
        self.assertEqual(response.status_code,400)
        self.assertIn("UTC offset",response.json()["error"])
        self.assertFalse(Punch.objects.exists())

    def test_future_device_clock_is_rejected(self):
        from datetime import timedelta
        from django.utils import timezone
        response=self.punch(occurred_at=(timezone.now()+timedelta(minutes=30)).isoformat())
        self.assertEqual(response.status_code,400)
        self.assertIn("ahead of the server clock",response.json()["error"])
        self.assertFalse(Punch.objects.exists())

    def test_stale_offline_punch_is_still_rejected(self):
        from datetime import timedelta
        from django.utils import timezone
        response=self.punch(occurred_at=(timezone.now()-timedelta(hours=13)).isoformat())
        self.assertEqual(response.status_code,400)
        self.assertIn("12 hours",response.json()["error"])

    def test_clock_in_without_a_scheduled_shift_awaits_review(self):
        response=self.punch(shift_id=None)
        self.assertEqual(response.status_code,201)
        punch=Punch.objects.get()
        self.assertEqual(punch.review_status,Punch.Review.PENDING)
        self.assertIn("not linked to a scheduled shift",punch.exception_reason)

    def test_geofence_applies_to_a_shiftless_punch_that_names_a_site(self):
        response=self.punch(shift_id=None,site_id=str(self.site.pk),latitude="33.500000",longitude="-97.500000")
        self.assertEqual(response.status_code,201)
        punch=Punch.objects.get()
        self.assertIn("outside the site geofence",punch.exception_reason)

    def test_missing_location_is_flagged_because_the_browser_sends_null(self):
        response=self.punch(latitude=None,longitude=None)
        self.assertEqual(response.status_code,201)
        self.assertIn("Location was not supplied",Punch.objects.get().exception_reason)

    def test_foreign_site_identifier_is_rejected(self):
        other=Organization.objects.create(legal_name="Other LLC",display_name="Other",slug="enforce-other")
        client=Client.objects.create(organization=other,name="Other client")
        site=Site.objects.create(organization=other,client=client,name="Other site",address="El Paso")
        response=self.punch(shift_id=None,site_id=str(site.pk))
        self.assertEqual(response.status_code,400)
        self.assertFalse(Punch.objects.exists())

    def test_suspended_credential_blocks_an_unscheduled_clock_in(self):
        self.credential.status=Credential.Status.SUSPENDED; self.credential.save(update_fields=["status"])
        response=self.punch(shift_id=None)
        self.assertEqual(response.status_code,400)
        self.assertIn("suspended",response.json()["error"])
        self.assertFalse(Punch.objects.exists())

    def test_revoked_credential_blocks_a_shift_that_does_not_require_it(self):
        from .models import CredentialType
        unrelated=CredentialType.objects.create(organization=self.org,name="Personal protection",code="ppo")
        Credential.objects.create(organization=self.org,person=self.person,credential_type=unrelated,status=Credential.Status.REVOKED)
        response=self.punch()
        self.assertEqual(response.status_code,400)
        self.assertIn("revoked",response.json()["error"])
        self.assertFalse(Punch.objects.exists())

    def test_operator_may_decline_to_clock_gate_a_credential_type(self):
        self.credential_type.blocks_clock_in=False; self.credential_type.save(update_fields=["blocks_clock_in"])
        self.credential.status=Credential.Status.SUSPENDED; self.credential.save(update_fields=["status"])
        response=self.punch(shift_id=None)
        self.assertEqual(response.status_code,201)

    def test_unclosed_clock_in_and_orphan_clock_out_are_flagged(self):
        self.assertEqual(self.punch().status_code,201)
        self.assertEqual(self.punch().status_code,201)
        second=Punch.objects.order_by("-occurred_at").first()
        self.assertIn("never closed with a clock-out",second.exception_reason)
        Punch.objects.all().delete()
        self.assertEqual(self.punch(kind=Punch.Kind.OUT).status_code,201)
        self.assertIn("no matching clock-in",Punch.objects.get().exception_reason)


class ShiftCsvImportTest(TestCase):
    def setUp(self):
        User=get_user_model(); self.user=User.objects.create_user(username="shift-import@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Shifts LLC",display_name="Shifts",slug="shifts")
        Membership.objects.create(user=self.user,organization=self.org,role=Membership.Role.ADMIN)
        Client.objects.create(organization=self.org,name="Hospital")
        Site.objects.create(organization=self.org,client=self.org.clients.get(),name="North Campus",address="Austin")
        self.client.force_login(self.user)

    def test_shift_rows_preview_and_apply(self):
        from .models import ImportBatch
        payload=b"client,site,starts_at,ends_at,post_name\nHospital,North Campus,2026-05-01T20:00:00Z,2026-05-02T04:00:00Z,Lobby\n"
        response=self.client.post(reverse("imports"),{"entity":"shifts","file":__import__("django.core.files.uploadedfile",fromlist=["SimpleUploadedFile"]).SimpleUploadedFile("shifts.csv",payload,content_type="text/csv")})
        self.assertEqual(response.status_code,200)
        batch=ImportBatch.objects.get()
        self.assertEqual(batch.status,ImportBatch.Status.PREVIEW)
        self.assertEqual(batch.errors,[])
        self.client.post(reverse("import_apply",args=[batch.pk]))
        self.assertEqual(Shift.objects.count(),1)
        self.assertEqual(ImportBatch.objects.get().status,ImportBatch.Status.APPLIED)

    def test_malformed_shift_timestamp_is_a_row_error_not_a_crash(self):
        from .models import ImportBatch
        payload=b"client,site,starts_at,ends_at\nHospital,North Campus,yesterday,tomorrow\n"
        response=self.client.post(reverse("imports"),{"entity":"shifts","file":__import__("django.core.files.uploadedfile",fromlist=["SimpleUploadedFile"]).SimpleUploadedFile("shifts.csv",payload,content_type="text/csv")})
        self.assertEqual(response.status_code,200)
        self.assertEqual(ImportBatch.objects.get().status,ImportBatch.Status.INVALID)


class AuditRedactionGovernanceTest(TestCase):
    def setUp(self):
        User=get_user_model()
        self.requester=User.objects.create_user(username="redact1@example.com",password="correct horse battery staple")
        self.approver=User.objects.create_user(username="redact2@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Redaction LLC",display_name="Redaction",slug="redaction")
        Membership.objects.create(user=self.requester,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.approver,organization=self.org,role=Membership.Role.ADMIN)
        self.event=AuditEvent.objects.create(organization=self.org,actor=self.requester,action="person.updated",target_type="person",target_id="1",metadata={"email":"guard@example.com","fields":["email"]})
        self.client.force_login(self.requester)

    def export(self):
        response=self.client.get(reverse("audit_export"))
        self.assertEqual(response.status_code,200)
        return response.content.decode()

    def test_exports_only_change_after_a_second_person_approves(self):
        self.assertIn("guard@example.com",self.export())
        response=self.client.post(reverse("audit_redact",args=[self.event.pk]),{"fields":"email","reason":"Employee exercised a deletion right","legal_basis":"Texas Occupations Code 1702"},follow=True)
        self.assertEqual(response.status_code,200)
        redaction=AuditRedaction.objects.get()
        self.assertEqual(redaction.status,AuditRedaction.Status.PENDING)
        self.assertIsNone(redaction.approved_by)
        self.assertIn("guard@example.com",self.export())
        self.client.post(reverse("audit_redaction_decide",args=[redaction.pk]),{"action":"applied"})
        redaction.refresh_from_db()
        self.assertEqual(redaction.status,AuditRedaction.Status.PENDING)
        self.client.force_login(self.approver)
        self.client.post(reverse("audit_redaction_decide",args=[redaction.pk]),{"action":"applied"})
        redaction.refresh_from_db()
        self.assertEqual(redaction.status,AuditRedaction.Status.APPLIED)
        self.assertEqual(redaction.approved_by_id,self.approver.pk)
        self.assertNotIn("guard@example.com",self.export())
        self.assertIn("[REDACTED]",self.export())

    def test_a_redaction_request_is_recorded_once_per_event(self):
        data={"fields":"email","reason":"Duplicate request guard","legal_basis":"Retention schedule"}
        self.client.post(reverse("audit_redact",args=[self.event.pk]),data)
        self.client.post(reverse("audit_redact",args=[self.event.pk]),data)
        self.assertEqual(AuditRedaction.objects.count(),1)

    def test_decision_requires_a_post_and_a_privileged_member(self):
        self.client.post(reverse("audit_redact",args=[self.event.pk]),{"fields":"email","reason":"Reject path coverage","legal_basis":"Retention schedule"})
        redaction=AuditRedaction.objects.get()
        self.assertEqual(self.client.get(reverse("audit_redaction_decide",args=[redaction.pk])).status_code,405)
        outsider=get_user_model().objects.create_user(username="redact3@example.com",password="correct horse battery staple")
        other=Organization.objects.create(legal_name="Outsider LLC",display_name="Outsider",slug="outsider")
        Membership.objects.create(user=outsider,organization=other,role=Membership.Role.OWNER)
        self.client.force_login(outsider)
        self.assertEqual(self.client.post(reverse("audit_redaction_decide",args=[redaction.pk]),{"action":"applied"}).status_code,404)


class AdminSurfaceTest(TestCase):
    def setUp(self):
        User=get_user_model(); self.user=User.objects.create_user(username="operator@example.com",password="correct horse battery staple")

    def test_admin_login_attempts_are_throttled(self):
        from django.core.cache import cache
        from django.test import override_settings
        cache.clear()
        with override_settings(ADMIN_ALLOWED_IPS=["127.0.0.1/32"]):
            for _ in range(5):
                self.assertEqual(self.client.post("/admin/login/",{"username":"operator","password":"wrong"}).status_code,200)
            blocked=self.client.post("/admin/login/",{"username":"operator","password":"wrong"})
        self.assertEqual(blocked.status_code,429)

    def test_admin_closes_when_no_source_range_is_configured(self):
        from django.test import override_settings
        with override_settings(ADMIN_ALLOWED_IPS=[],DEBUG=False):
            self.assertEqual(self.client.get("/admin/").status_code,403)
            self.assertEqual(self.client.get("/").status_code,302)

    def test_admin_allows_only_a_configured_source(self):
        from django.test import override_settings
        with override_settings(ADMIN_ALLOWED_IPS=["203.0.113.0/24"],DEBUG=False):
            self.assertEqual(self.client.get("/admin/",REMOTE_ADDR="198.51.100.7").status_code,403)
            self.assertEqual(self.client.get("/admin/login/",REMOTE_ADDR="203.0.113.9").status_code,200)

    def test_development_keeps_the_admin_open(self):
        from django.test import override_settings
        with override_settings(ADMIN_ALLOWED_IPS=[],DEBUG=True):
            self.assertEqual(self.client.get("/admin/",REMOTE_ADDR="198.51.100.7").status_code,302)

    def test_clients_behind_the_proxy_get_independent_login_buckets(self):
        from django.core.cache import cache
        from django.test import override_settings
        cache.clear()
        with override_settings(TRUSTED_PROXIES=["172.18.0.0/16"]):
            for _ in range(5):
                self.client.post(reverse("account_login"),{"login":"tenant-a@example.com","password":"wrong"},REMOTE_ADDR="172.18.0.5",HTTP_X_FORWARDED_FOR="203.0.113.1")
            self.assertEqual(self.client.post(reverse("account_login"),{"login":"tenant-a@example.com","password":"wrong"},REMOTE_ADDR="172.18.0.5",HTTP_X_FORWARDED_FOR="203.0.113.1").status_code,429)
            neighbour=self.client.post(reverse("account_login"),{"login":"tenant-b@example.com","password":"wrong"},REMOTE_ADDR="172.18.0.5",HTTP_X_FORWARDED_FOR="198.51.100.2")
            self.assertEqual(neighbour.status_code,200)

    def test_a_direct_client_cannot_forge_forwarded_headers(self):
        from django.core.cache import cache
        from django.test import override_settings
        cache.clear()
        with override_settings(TRUSTED_PROXIES=["172.18.0.0/16"]):
            for _ in range(5):
                self.client.post(reverse("account_login"),{"login":"victim@example.com","password":"wrong"},REMOTE_ADDR="198.51.100.7",HTTP_X_FORWARDED_FOR="203.0.113.1")
            self.assertEqual(self.client.post(reverse("account_login"),{"login":"victim@example.com","password":"wrong"},REMOTE_ADDR="198.51.100.7",HTTP_X_FORWARDED_FOR="203.0.113.1").status_code,429)
            victim=self.client.post(reverse("account_login"),{"login":"victim@example.com","password":"wrong"},REMOTE_ADDR="203.0.113.1")
            self.assertEqual(victim.status_code,200)


class ExportFormulaSafetyTest(SimpleTestCase):
    def test_person_supplied_text_cannot_become_a_formula(self):
        import io, zipfile
        from .services import payroll_snapshot_csv, payroll_snapshot_xlsx, safe_cell
        rows=[{"employee_id":"=1+1","employee":"+441234","regular_hours":"8.00","overtime_hours":"0.00","total_hours":"8.00","exception":"@cmd"}]
        csv_text=payroll_snapshot_csv(rows).decode()
        self.assertIn("'=1+1",csv_text); self.assertIn("'@cmd",csv_text)
        self.assertNotIn("\n=1+1",csv_text)
        with zipfile.ZipFile(io.BytesIO(payroll_snapshot_xlsx(rows))) as archive:
            sheet=archive.read("xl/worksheets/sheet1.xml")
        self.assertIn(b"'=1+1",sheet)
        self.assertIn(b"'+441234",sheet)
        self.assertEqual(safe_cell("-45"),"'-45"); self.assertEqual(safe_cell("8.00"),"8.00")


class BrandLogoTest(TestCase):
    def setUp(self):
        import tempfile
        from django.core.files.base import ContentFile
        from django.test import override_settings
        from PIL import Image
        User=get_user_model(); self.user=User.objects.create_user(username="brand@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Brand LLC",display_name="Brand",slug="brand")
        Membership.objects.create(user=self.user,organization=self.org,role=Membership.Role.OFFICER)
        self.temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.override=override_settings(MEDIA_ROOT=self.temp.name); self.override.enable()
        image=Image.new("RGBA",(24,24),"white")
        from io import BytesIO
        buffer=BytesIO(); image.save(buffer,format="PNG")
        self.org.logo.save("brand.png",ContentFile(buffer.getvalue()),save=True)

    def tearDown(self):
        # No explicit logo delete: the streamed response may still hold the handle open on
        # Windows, and the temporary tree is removed tolerantly below anyway.
        self.override.disable(); self.temp.cleanup()

    def test_logo_is_streamed_from_an_authenticated_same_origin_route(self):
        response=self.client.get(reverse("brand_logo"))
        self.assertEqual(response.status_code,302)
        self.client.force_login(self.user)
        response=self.client.get(reverse("brand_logo"))
        self.assertEqual(response.status_code,200)
        self.assertTrue(response["Content-Type"].startswith("image/"))
        self.assertEqual(response["X-Content-Type-Options"],"nosniff")
        self.assertNotIn("https://media",response.headers.get("Content-Security-Policy",""))

    def test_templates_point_at_the_route_not_at_a_media_url(self):
        from pathlib import Path
        for template in (Path("templates/base.html"),Path("templates/core/branding.html")):
            self.assertNotIn("logo.url",template.read_text(),template)


class ProvisionedAccountMfaTest(TestCase):
    """allauth refuses to enroll an authenticator while the user holds an unverified address.
    Required MFA redirects that user to the enrollment page on every request, so an
    unverified provisioned account is a dead end rather than a warning."""

    def assert_enrollment_allowed(self, user):
        from allauth.mfa.internal.flows.add import validate_can_add_authenticator
        validate_can_add_authenticator(user)

    def test_unverified_address_blocks_enrollment(self):
        from allauth.account.models import EmailAddress
        user=get_user_model().objects.create_user(username="unverified@example.com",email="unverified@example.com",password="correct horse battery staple")
        EmailAddress.objects.create(user=user, email=user.email, verified=False, primary=True)
        with self.assertRaises(ValidationError):
            self.assert_enrollment_allowed(user)

    def test_bootstrap_owner_can_enroll_mfa(self):
        import os
        from io import StringIO
        from unittest.mock import patch
        from allauth.account.models import EmailAddress
        from django.core.management import call_command
        with patch.dict(os.environ, {"BOOTSTRAP_EMAIL":"ops@example.com","BOOTSTRAP_PASSWORD":"a sufficiently long passphrase","BOOTSTRAP_COMPANY":"Ops LLC","BOOTSTRAP_SLUG":"ops-provisioned"}):
            call_command("bootstrap_admin", stdout=StringIO())
        user=get_user_model().objects.get(username="ops@example.com")
        self.assertTrue(EmailAddress.objects.filter(user=user, verified=True).exists())
        self.assert_enrollment_allowed(user)

    def test_invited_admin_can_enroll_mfa(self):
        from allauth.account.models import EmailAddress
        User=get_user_model(); owner=User.objects.create_user(username="inviter2@example.com",password="correct horse battery staple")
        org=Organization.objects.create(legal_name="Invite LLC",display_name="Invite",slug="invite-provisioned")
        Membership.objects.create(user=owner,organization=org,role=Membership.Role.OWNER)
        self.client.force_login(owner)
        issued=self.client.post(reverse("team"),{"email":"new.admin@example.com","role":Membership.Role.ADMIN},follow=True)
        token=issued.context["invitation_url"].rstrip("/").rsplit("/",1)[-1]
        self.client.logout()
        accepted=self.client.post(reverse("invitation_accept",args=[token]),{"first_name":"New","last_name":"Admin","password":"a sufficiently long passphrase","password_confirmation":"a sufficiently long passphrase"})
        self.assertRedirects(accepted, reverse("dashboard"))
        user=User.objects.get(email="new.admin@example.com")
        self.assertTrue(EmailAddress.objects.filter(user=user, verified=True).exists())
        self.assert_enrollment_allowed(user)


class StaffMfaTest(TestCase):
    def test_platform_accounts_must_enroll_a_second_factor(self):
        from django.utils import timezone
        User=get_user_model(); staff=User.objects.create_user(username="platform@example.com",password="correct horse battery staple",is_staff=True)
        self.client.force_login(staff)
        response=self.client.get(reverse("dashboard"))
        self.assertRedirects(response,reverse("mfa_activate_totp"),fetch_redirect_response=False)
        health=self.client.get(reverse("health"))
        self.assertEqual(health.status_code,302)
        staff.is_staff=False; staff.save(update_fields=["is_staff"])
        self.assertEqual(self.client.get(reverse("health")).status_code,200)


class PeopleCentricRecordsTest(TestCase):
    """The personnel profile is where credentials, training, and documents are created.

    The business process is "onboard this guard", not "upload a file, then say who it is
    for". The company-record case is the mirror image: a handbook has no person, yet every
    worker still has to receive and acknowledge it.
    """

    def setUp(self):
        import tempfile
        from django.test import override_settings
        User=get_user_model()
        self.owner=User.objects.create_user(username="hub-owner@example.com",password="correct horse battery staple")
        self.officer=User.objects.create_user(username="hub-officer@example.com",password="correct horse battery staple")
        self.scheduler=User.objects.create_user(username="hub-scheduler@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Hub LLC",display_name="Hub",slug="hub")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.officer,organization=self.org,role=Membership.Role.OFFICER)
        Membership.objects.create(user=self.scheduler,organization=self.org,role=Membership.Role.SCHEDULER)
        self.person=Person.objects.create(organization=self.org,user=self.officer,first_name="Taylor",last_name="Reed",status=Person.Status.ACTIVE,is_commissioned_officer=True)
        self.other=Person.objects.create(organization=self.org,first_name="Sam",last_name="Other",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        self.credential_type=CredentialType.objects.create(organization=self.org,name="Firearms discharge licence",code="handgun",applies_to=["commissioned"],blocks_scheduling=True,blocks_clock_in=True)
        self.personal_type=DocumentType.objects.create(organization=self.org,name="Background screening",code="screening")
        self.handbook_type=DocumentType.objects.create(organization=self.org,name="Employee handbook",code="handbook",audience=DocumentType.Audience.WORKFORCE,acknowledgment_required=True)
        self.licence_type=DocumentType.objects.create(organization=self.org,name="Company licence",code="company-licence",audience=DocumentType.Audience.MANAGEMENT)
        media=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        override=override_settings(MEDIA_ROOT=media.name); override.enable()
        self.addCleanup(override.disable); self.addCleanup(media.cleanup)

    def sign_in(self,user):
        self.client.force_login(user)

    def assert_created(self,response):
        """302 is success; a re-rendered form carries the reason it refused to save."""
        if response.status_code==302: return
        form=response.context.get("form") if response.context else None
        self.fail("expected redirect, got %s: %s"%(response.status_code,form.errors if form else "no form in context"))

    def upload(self,**fields):
        from django.core.files.uploadedfile import SimpleUploadedFile
        data={"document_type":fields["document_type"].pk,"file":SimpleUploadedFile("record.pdf",b"%PDF-1.4\nbody",content_type="application/pdf")}
        if "person" in fields: data["person"]=fields["person"].pk
        return data

    def test_profile_tabs_render_the_records_that_belong_to_the_person(self):
        from datetime import timedelta
        from django.utils import timezone
        Credential.objects.create(organization=self.org,person=self.person,credential_type=self.credential_type,status=Credential.Status.ACTIVE,expires_on=timezone.localdate()+timedelta(days=300))
        TrainingRecord.objects.create(organization=self.org,person=self.person,course_name="Level III handgun",completed_on=timezone.localdate()-timedelta(days=10))
        self.sign_in(self.owner)
        base=reverse("person_detail",args=[self.person.pk])
        self.assertContains(self.client.get(base+"?tab=credentials"),"Firearms discharge licence")
        self.assertContains(self.client.get(base+"?tab=training"),"Level III handgun")
        self.assertContains(self.client.get(base+"?tab=documents"),"Upload document")

    def test_database_guard_still_rejects_a_foreign_record_type_for_a_company_record(self):
        """The NULL-person case, asserted at the database rather than in Python.

        Making person nullable rewrote core_document_tenant_insert. The old body compared
        a subselect that yields NULL for a NULL person, so its behaviour on the new rows was
        only accidental — this pins both halves: a company record inserts, and a company
        record borrowing another tenant's document type is still refused by the server.
        """
        from django.db import connection
        if connection.vendor!="mysql":
            self.skipTest("the tenant guards are MySQL triggers")
        other=Organization.objects.create(legal_name="Foreign LLC",display_name="Foreign",slug="foreign-doc")
        foreign_type=DocumentType.objects.create(organization=other,name="Foreign record",code="foreign-record")
        with connection.cursor() as cursor:
            cursor.execute("SELECT ACTION_STATEMENT FROM information_schema.triggers WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='core_document_tenant_insert' AND EVENT_OBJECT_TABLE='core_persondocument' LIMIT 1")
            definition=cursor.fetchone()[0]
        self.assertIn("NEW.person_id IS NOT NULL",definition)
        import uuid
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO core_persondocument (id,organization_id,person_id,document_type_id,file,original_name,content_type,size,sha256,scan_status,legal_hold,archived_at,acknowledged_at,deleted_at,created_at) VALUES (%s,%s,NULL,%s,'','x.pdf','application/pdf',1,%s,'clean',0,NULL,NULL,NULL,NOW(6))",
                    [uuid.uuid4().hex,str(self.org.pk).replace("-",""),str(foreign_type.pk).replace("-",""),"0"*64])
        except Exception as exc:
            self.assertIn("cross-tenant document reference",str(exc))
        else:
            self.fail("the MySQL guard accepted a company record borrowing another tenant's document type")
        own_type=DocumentType.objects.create(organization=self.org,name="Own handbook",code="own-handbook")
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core_persondocument (id,organization_id,person_id,document_type_id,file,original_name,content_type,size,sha256,scan_status,legal_hold,archived_at,acknowledged_at,deleted_at,created_at) VALUES (%s,%s,NULL,%s,'','h.pdf','application/pdf',1,%s,'clean',0,NULL,NULL,NULL,NOW(6))",
                [uuid.uuid4().hex,str(self.org.pk).replace("-",""),str(own_type.pk).replace("-",""),"1"*64])
            self.assertEqual(cursor.rowcount,1)

    def test_branches_are_listed_with_what_is_attached_to_them(self):
        """Branches previously had a create route and no view at all."""
        branch=Branch.objects.create(organization=self.org,name="Dallas")
        self.sign_in(self.owner)
        page=self.client.get(reverse("branches"))
        self.assertContains(page,"Dallas")
        self.assertContains(page,"No sites attached")
        self.assertContains(page,"No personnel assigned")
        self.client.post(reverse("branch_create"),{"name":"Austin","city":"Austin"})
        self.client.post(reverse("site_create"),{"client":Client.objects.create(organization=self.org,name="Bank").pk,"branch":branch.pk,"name":"Vault","address":"Dallas TX","geofence_radius_meters":200,"active":"on"})
        updated=self.client.get(reverse("branches"))
        self.assertContains(updated,"Dallas")

    def test_scoped_create_forms_accept_their_own_valid_input(self):
        """Regression: these four forms could never save.

        The view assigned the organization after is_valid(), but ModelForm._post_clean runs
        the model's cross-tenant guard during validation against an instance whose
        organization_id was still None — so "Client must belong to the same organization"
        fired on correct data for sites, shifts, credentials, and training alike.
        """
        from datetime import timedelta
        from django.utils import timezone
        self.sign_in(self.owner)
        client=Client.objects.create(organization=self.org,name="Stadium")
        branch=Branch.objects.create(organization=self.org,name="Dallas")
        site=self.client.post(reverse("site_create"),{"client":client.pk,"branch":branch.pk,"name":"North gate","address":"Dallas TX","latitude":"32.776700","longitude":"-96.797000","geofence_radius_meters":180,"active":"on"})
        self.assert_created(site)
        created=Site.objects.get(name="North gate")
        shift=self.client.post(reverse("shift_create"),{"site":created.pk,"officer":self.person.pk,"starts_at":(timezone.now()+timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),"ends_at":(timezone.now()+timedelta(days=1,hours=8)).strftime("%Y-%m-%dT%H:%M"),"status":Shift.Status.DRAFT,"post_name":"Gate 1","post_orders":"","required_credentials":[self.credential_type.pk]})
        self.assert_created(shift)
        credential=self.client.post(reverse("credential_create"),{"person":self.person.pk,"credential_type":self.credential_type.pk,"status":Credential.Status.ACTIVE,"number":"B-77"})
        self.assert_created(credential)
        training=self.client.post(reverse("training_create"),{"person":self.person.pk,"course_name":"State level III","provider":"Range","completed_on":timezone.localdate().isoformat(),"certificate_number":"","hours":"8"})
        self.assert_created(training)
        self.assertEqual(Site.objects.filter(organization=self.org).count(),1)
        self.assertEqual(Shift.objects.filter(site=created).count(),1)
        self.assertEqual(Credential.objects.filter(person=self.person).count(),1)
        self.assertEqual(TrainingRecord.objects.filter(person=self.person).count(),1)

    def test_a_foreign_client_is_still_rejected(self):
        """The fix must not have loosened the guard: the choices are tenant-scoped."""
        other=Organization.objects.create(legal_name="Rival LLC",display_name="Rival",slug="rival")
        foreign=Client.objects.create(organization=other,name="Rival client")
        self.sign_in(self.owner)
        response=self.client.post(reverse("site_create"),{"client":foreign.pk,"branch":"","name":"Gate","address":"Dallas TX","geofence_radius_meters":200,"active":"on"})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"Select a valid choice")
        self.assertEqual(Site.objects.count(),0)

    def test_records_created_from_a_profile_cannot_be_filed_against_someone_else(self):
        self.sign_in(self.owner)
        response=self.client.post(reverse("person_credential_create",args=[self.person.pk]),{"credential_type":self.credential_type.pk,"status":Credential.Status.ACTIVE,"number":"B-12345","person":self.other.pk})
        self.assert_created(response)
        self.assertEqual(Credential.objects.filter(person=self.person).count(),1)
        self.assertEqual(Credential.objects.filter(person=self.other).count(),0)

    def test_a_personless_requirement_produces_no_missing_row(self):
        self.sign_in(self.owner)
        scoped=self.client.get(reverse("compliance")+"?kind=credentials&show=all")
        self.assertContains(scoped,"Firearms discharge licence")
        # The unarmed officer is outside the requirement's applicability, so no obligation.
        unarmed=self.client.get(reverse("compliance")+"?kind=credentials&show=all&person=%s"%self.other.pk)
        self.assertNotContains(unarmed,"No record filed for an applicable requirement")

    def test_company_record_reaches_every_worker_and_can_be_acknowledged(self):
        self.sign_in(self.owner)
        response=self.client.post(reverse("document_upload"),self.upload(document_type=self.handbook_type))
        self.assertEqual(response.status_code,302)
        document=PersonDocument.objects.get(document_type=self.handbook_type)
        self.assertIsNone(document.person)
        self.sign_in(self.officer)
        self.assertContains(self.client.get(reverse("my_documents")),"Employee handbook")
        acknowledged=self.client.post(reverse("document_acknowledge",args=[document.pk]),{"confirm":"on","signature_name":"Taylor Reed"})
        self.assertEqual(acknowledged.status_code,302)
        from .models import DocumentAcknowledgment
        self.assertEqual(DocumentAcknowledgment.objects.filter(document=document,person=self.person).count(),1)
        self.assertEqual(self.client.get(reverse("document_download",args=[document.pk])).status_code,200)

    def test_a_management_company_record_is_not_released_to_workers(self):
        from .services import store_person_document
        document=store_person_document(organization=self.org,person=None,document_type=self.licence_type,upload=__import__("django.core.files.uploadedfile",fromlist=["SimpleUploadedFile"]).SimpleUploadedFile("psb.pdf",b"%PDF-1.4\nlicence",content_type="application/pdf"),actor=self.owner)
        self.sign_in(self.officer)
        self.assertNotContains(self.client.get(reverse("my_documents")),"Company licence")
        self.assertEqual(self.client.get(reverse("document_download",args=[document.pk])).status_code,404)
        self.sign_in(self.owner)
        self.assertEqual(self.client.get(reverse("document_download",args=[document.pk])).status_code,200)

    def test_a_personnel_record_type_still_requires_a_person(self):
        self.sign_in(self.owner)
        response=self.client.post(reverse("document_upload"),self.upload(document_type=self.personal_type))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"belongs in a personnel file")

    def test_scheduler_reads_credential_state_but_not_the_private_file(self):
        Credential.objects.create(organization=self.org,person=self.person,credential_type=self.credential_type,status=Credential.Status.SUSPENDED,number="B-1")
        self.sign_in(self.scheduler)
        self.assertEqual(self.client.get(reverse("compliance")).status_code,200)
        self.assertEqual(self.client.get(reverse("documents")).status_code,403)
        self.assertEqual(self.client.get(reverse("person_detail",args=[self.person.pk])+"?tab=documents").status_code,404)
        profile=self.client.get(reverse("person_detail",args=[self.person.pk])+"?tab=credentials")
        self.assertContains(profile,"Firearms discharge licence")
        self.assertContains(profile,"Suspended")

    def test_orphaned_configuration_pages_are_reachable_from_settings(self):
        self.sign_in(self.owner)
        page=self.client.get(reverse("settings"))
        for target in ("/settings/security/","/settings/time/","/people/fields/new/","/settings/compliance/","/documents/retention/","/settings/branding/","/settings/domains/"):
            self.assertContains(page,target)

    def test_officer_navigation_omits_surfaces_that_would_refuse_them(self):
        self.sign_in(self.officer)
        dashboard=self.client.get(reverse("dashboard"))
        self.assertContains(dashboard,"/clock/")
        self.assertContains(dashboard,"/my-documents/")
        for absent in ("/payroll/","/audit/","/team/","/documents/"):
            self.assertNotContains(dashboard,absent)

    def test_owner_sidebar_keeps_account_controls_after_the_last_section(self):
        self.sign_in(self.owner)
        dashboard=self.client.get(reverse("dashboard"))
        for section in ("Quick access","Today","People","Schedule","Time &amp; Payroll","Compliance &amp; Records","Reports","Settings"):
            self.assertContains(dashboard,section)
        self.assertContains(dashboard,'action="/accounts/logout/"')
        self.assertContains(dashboard,"Sign out")

    def test_training_register_and_queue_only_offer_allowed_roles(self):
        self.sign_in(self.officer)
        self.assertEqual(self.client.get(reverse("training")).status_code,403)
        self.assertEqual(self.client.get(reverse("settings_compliance")).status_code,403)


class NotificationDeliveryCommandTest(TestCase):
    def setUp(self):
        from .models import Notification
        User=get_user_model(); self.user=User.objects.create_user(username="worker-notify@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Queue LLC",display_name="Queue",slug="queue")
        Membership.objects.create(user=self.user,organization=self.org,role=Membership.Role.OWNER)

    def test_command_delivers_queued_notices_and_respects_attempt_limits(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import Notification
        ready=Notification.objects.create(organization=self.org,recipient=self.user,channel=Notification.Channel.IN_APP,event_type="test",subject="Ready",body="body")
        exhausted=Notification.objects.create(organization=self.org,recipient=self.user,channel=Notification.Channel.IN_APP,event_type="test",subject="Exhausted",body="body",status=Notification.Status.FAILED,attempts=5)
        out=StringIO(); call_command("process_notifications",stdout=out)
        ready.refresh_from_db(); exhausted.refresh_from_db()
        self.assertEqual(ready.status,Notification.Status.SENT)
        self.assertEqual(exhausted.status,Notification.Status.FAILED)
        self.assertEqual(exhausted.attempts,5)
        self.assertIn("sent=1",out.getvalue())


class SignInProvisioningTest(TestCase):
    """A guard has to be able to reach the clock without a Django-admin edit.

    ``Person.user`` gates the time clock, the officer's own document queue, and correction
    requests. Production code never set it, so a provisioned workforce dead-ended at
    "Personnel link required" while /admin/ is closed to every source address.
    """

    def setUp(self):
        import re
        self.re=re
        User=get_user_model(); self.hr=User.objects.create_user(username="provision-hr@example.com",email="provision-hr@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Provision LLC",display_name="Provision",slug="provision")
        Membership.objects.create(user=self.hr,organization=self.org,role=Membership.Role.HR)
        self.person=Person.objects.create(organization=self.org,first_name="Robin",last_name="Adams",email="robin@example.com",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        self.client.force_login(self.hr)

    def invitation_path(self):
        from .models import Notification
        body=Notification.objects.filter(channel=Notification.Channel.EMAIL,destination="robin@example.com").get().body
        return self.re.search(r"(https?://testserver/invitations/[^ ]+)",body).group(1)

    def test_invitation_from_the_profile_links_the_sign_in_and_opens_the_clock(self):
        self.person.user=None; self.person.save()
        response=self.client.post(reverse("person_access_invite",args=[self.person.pk]),{"role":Membership.Role.OFFICER})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"Invitation sent")
        from .models import MembershipInvitation
        invitation=MembershipInvitation.objects.get(person=self.person)
        self.assertEqual(invitation.role,Membership.Role.OFFICER)
        self.client.logout()
        accepted=self.client.get(self.invitation_path())
        self.assertEqual(accepted.status_code,200)
        created=self.client.post(self.invitation_path(),{"first_name":"Robin","last_name":"Adams","password":"a-long-guard-password","password_confirmation":"a-long-guard-password"})
        self.assertRedirects(created,reverse("dashboard"))
        self.person.refresh_from_db()
        self.assertIsNotNone(self.person.user)
        self.assertEqual(self.person.user.email,"robin@example.com")
        officer_page=self.client.get(reverse("clock"))
        self.assertContains(officer_page,"Clock in")
        self.assertEqual(self.client.get(reverse("people")).status_code,403)   # officer access stays scoped to their own work
        self.client.logout()

    def test_an_invitation_never_repoints_a_record_that_already_has_a_sign_in(self):
        from .models import MembershipInvitation
        other=get_user_model().objects.create_user(username="existing@example.com",email="existing@example.com",password="correct horse battery staple")
        self.client.post(reverse("person_access_invite",args=[self.person.pk]),{"role":Membership.Role.OFFICER})
        invitation=MembershipInvitation.objects.select_related("person").get(person=self.person)
        # The record gets linked while the invitation is still out; a later applicant must
        # not be able to move it by accepting the stale link.
        self.person.user=other; self.person.save()
        applicant=get_user_model().objects.create_user(username="applicant@example.com",email="applicant@example.com",password="applicant-password-1")
        from .views import _link_invited_person
        from django.db import transaction
        with transaction.atomic():   # the real caller holds invitation_accept's transaction
            problem=_link_invited_person(invitation,applicant)
        self.assertIsNotNone(problem)
        self.person.refresh_from_db()
        self.assertEqual(self.person.user_id,other.pk)
        self.assertTrue(AuditEvent.objects.filter(action="person.signin_conflict").exists())

    def test_hiring_manager_cannot_mint_an_owner_from_a_personnel_profile(self):
        response=self.client.post(reverse("person_access_invite",args=[self.person.pk]),{"role":Membership.Role.OWNER})
        self.assertEqual(response.status_code,200)
        self.assertIn("role",response.context["form"].errors)
        from .models import MembershipInvitation
        self.assertFalse(MembershipInvitation.objects.exists())


class TeamAccessChangeTest(TestCase):
    """Roles move only on Team access; invitations and record links never change one."""

    def setUp(self):
        User=get_user_model()
        self.org=Organization.objects.create(legal_name="Access LLC",display_name="Access",slug="access")
        self.owner=User.objects.create_user(username="owner@access.test",email="owner@access.test",first_name="Olive",last_name="Owner")
        self.admin=User.objects.create_user(username="admin@access.test",email="admin@access.test")
        self.hr=User.objects.create_user(username="hr@access.test",email="hr@access.test")
        self.guard=User.objects.create_user(username="guard@access.test",email="guard@access.test")
        self.owner_m=Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        self.admin_m=Membership.objects.create(user=self.admin,organization=self.org,role=Membership.Role.ADMIN)
        self.hr_m=Membership.objects.create(user=self.hr,organization=self.org,role=Membership.Role.HR)
        self.guard_m=Membership.objects.create(user=self.guard,organization=self.org,role=Membership.Role.SUPERVISOR)
        self.person=Person.objects.create(organization=self.org,first_name="Olive",last_name="Owner",email="owner@access.test",status=Person.Status.ACTIVE)

    def change(self,actor,membership,role,active=True):
        self.client.force_login(actor)
        data={"role":role}
        if active: data["active"]="on"
        return self.client.post(reverse("membership_access",args=[membership.pk]),data)

    def test_profile_invitation_accepted_by_an_owner_keeps_owner(self):
        from .models import MembershipInvitation
        self.client.force_login(self.hr)
        self.client.post(reverse("person_access_invite",args=[self.person.pk]),{"role":Membership.Role.OFFICER})
        invitation=MembershipInvitation.objects.get(person=self.person)
        # The token is only in the queued email body.
        from .models import Notification
        import re
        url=re.search(r"(https?://testserver/invitations/[^ ]+)",Notification.objects.get(destination="owner@access.test").body).group(1)
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.post(url),reverse("dashboard"),fetch_redirect_response=False)
        self.owner_m.refresh_from_db(); self.person.refresh_from_db()
        self.assertEqual(self.owner_m.role,Membership.Role.OWNER)
        self.assertEqual(self.person.user_id,self.owner.pk)
        event=AuditEvent.objects.get(action="membership.invitation_accepted")
        self.assertTrue(event.metadata["role_kept"]); self.assertEqual(event.metadata["invited_role"],Membership.Role.OFFICER)

    def test_link_existing_member_sets_the_record_without_touching_the_role(self):
        self.client.force_login(self.owner)
        response=self.client.post(reverse("person_access_link",args=[self.person.pk]),{"member":self.owner_m.pk})
        self.assertRedirects(response,reverse("person_detail",args=[self.person.pk]),fetch_redirect_response=False)
        self.person.refresh_from_db(); self.owner_m.refresh_from_db()
        self.assertEqual(self.person.user_id,self.owner.pk)
        self.assertEqual(self.owner_m.role,Membership.Role.OWNER)
        self.assertTrue(AuditEvent.objects.filter(action="person.signin_linked",target_id=str(self.person.pk)).exists())

    def test_hr_cannot_link_a_record_to_an_owner_or_admin(self):
        self.client.force_login(self.hr)
        page=self.client.get(reverse("person_access_link",args=[self.person.pk]))
        offered=set(page.context["form"].fields["member"].queryset)
        self.assertNotIn(self.owner_m,offered); self.assertNotIn(self.admin_m,offered); self.assertIn(self.guard_m,offered)
        response=self.client.post(reverse("person_access_link",args=[self.person.pk]),{"member":self.owner_m.pk})
        self.assertEqual(response.status_code,200)
        self.person.refresh_from_db(); self.assertIsNone(self.person.user_id)

    def test_already_linked_members_are_not_offered(self):
        Person.objects.create(organization=self.org,first_name="G",last_name="Uard",status=Person.Status.ACTIVE,user=self.guard)
        self.client.force_login(self.owner)
        page=self.client.get(reverse("person_access_link",args=[self.person.pk]))
        self.assertNotIn(self.guard_m,set(page.context["form"].fields["member"].queryset))

    def test_promote_and_demote_a_supervisor_clears_unused_authority(self):
        from .models import Branch, AuthorityScope
        branch=Branch.objects.create(organization=self.org,name="North")
        AuthorityScope.objects.create(organization=self.org,membership=self.guard_m,branch=branch)
        self.assertRedirects(self.change(self.admin,self.guard_m,Membership.Role.HR),reverse("team"),fetch_redirect_response=False)
        self.guard_m.refresh_from_db()
        self.assertEqual(self.guard_m.role,Membership.Role.HR)
        self.assertFalse(AuthorityScope.objects.filter(membership=self.guard_m).exists())
        event=AuditEvent.objects.get(action="membership.access_changed")
        self.assertEqual(event.metadata["before"]["role"],Membership.Role.SUPERVISOR)
        self.assertEqual(event.metadata["scopes_removed"],["North branch"])
        self.change(self.admin,self.guard_m,Membership.Role.OFFICER)
        self.guard_m.refresh_from_db(); self.assertEqual(self.guard_m.role,Membership.Role.OFFICER)

    def test_deactivate_and_reactivate_a_member(self):
        self.change(self.owner,self.guard_m,Membership.Role.SUPERVISOR,active=False)
        self.guard_m.refresh_from_db(); self.assertFalse(self.guard_m.active)
        self.client.force_login(self.guard)
        self.assertEqual(self.client.get(reverse("clock")).status_code,403)
        self.change(self.owner,self.guard_m,Membership.Role.SUPERVISOR,active=True)
        self.guard_m.refresh_from_db(); self.assertTrue(self.guard_m.active)

    def test_admin_cannot_grant_owner_or_change_an_owner(self):
        response=self.change(self.admin,self.guard_m,Membership.Role.OWNER)
        self.assertEqual(response.status_code,200); self.assertIn("role",response.context["form"].errors)
        self.assertRedirects(self.change(self.admin,self.owner_m,Membership.Role.OFFICER),reverse("team"),fetch_redirect_response=False)
        self.owner_m.refresh_from_db(); self.assertEqual(self.owner_m.role,Membership.Role.OWNER)

    def test_the_last_owner_cannot_be_demoted_or_deactivated(self):
        response=self.change(self.owner,self.owner_m,Membership.Role.ADMIN)
        self.assertEqual(response.status_code,200); self.assertIn("role",response.context["form"].errors)
        self.owner_m.refresh_from_db(); self.assertEqual(self.owner_m.role,Membership.Role.OWNER)
        self.change(self.owner,self.admin_m,Membership.Role.OWNER)
        self.assertRedirects(self.change(self.owner,self.owner_m,Membership.Role.ADMIN),reverse("team"),fetch_redirect_response=False)
        self.owner_m.refresh_from_db(); self.assertEqual(self.owner_m.role,Membership.Role.ADMIN)

    def test_nobody_deactivates_themselves(self):
        response=self.change(self.admin,self.admin_m,Membership.Role.ADMIN,active=False)
        self.assertEqual(response.status_code,200); self.assertIn("active",response.context["form"].errors)
        self.admin_m.refresh_from_db(); self.assertTrue(self.admin_m.active)

    def test_hr_cannot_open_team_access_changes(self):
        self.assertEqual(self.change(self.hr,self.guard_m,Membership.Role.OFFICER).status_code,403)


class CatalogRemovalTest(TestCase):
    """Record types and personnel fields delete only when nothing uses them; duties always delete."""

    def setUp(self):
        import tempfile
        from django.test import override_settings
        from .models import CustomFieldDefinition, OnboardingItem, PersonCustomValue, PersonDocument, RuleRevision
        from .services import record_rule_revision
        self.CustomFieldDefinition,self.OnboardingItem,self.PersonCustomValue,self.PersonDocument,self.RuleRevision=CustomFieldDefinition,OnboardingItem,PersonCustomValue,PersonDocument,RuleRevision
        User=get_user_model()
        self.org=Organization.objects.create(legal_name="Catalog LLC",display_name="Catalog",slug="catalog")
        self.other=Organization.objects.create(legal_name="Other LLC",display_name="Other",slug="other-catalog")
        self.owner=User.objects.create_user(username="owner@catalog.test",email="owner@catalog.test")
        self.hr=User.objects.create_user(username="hr@catalog.test",email="hr@catalog.test")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.hr,organization=self.org,role=Membership.Role.HR)
        self.person=Person.objects.create(organization=self.org,first_name="Ray",last_name="Ortiz",status=Person.Status.ACTIVE)
        self.unused_type=DocumentType.objects.create(organization=self.org,name="Spare form",code="spare")
        self.filed_type=DocumentType.objects.create(organization=self.org,name="Background check",code="bg")
        self.field=CustomFieldDefinition.objects.create(organization=self.org,name="Locker",key="locker",kind="text")
        self.flag=CustomFieldDefinition.objects.create(organization=self.org,name="Has vehicle",key="vehicle",kind="boolean")
        self.duty=ComplianceRule.objects.create(organization=self.org,name="Posted licence",code="posting",
            evidence=ComplianceRule.Evidence.DOCUMENT,applies_to_subject=ComplianceRule.Subject.ORGANIZATION)
        record_rule_revision(self.duty,RuleRevision.Kind.COMPLIANCE_RULE,self.owner)
        media=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        override=override_settings(MEDIA_ROOT=media.name); override.enable()
        self.addCleanup(override.disable); self.addCleanup(media.cleanup)

    def file_under(self,document_type):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.PersonDocument.objects.create(organization=self.org,person=self.person,document_type=document_type,
            file=SimpleUploadedFile("bg.pdf",b"%PDF-1.4\nx",content_type="application/pdf"),original_name="bg.pdf",
            content_type="application/pdf",size=10,sha256="0"*64,scan_status=self.PersonDocument.ScanStatus.CLEAN)

    def test_an_unused_record_type_is_deleted(self):
        self.client.force_login(self.hr)
        self.assertRedirects(self.client.post(reverse("document_type_remove",args=[self.unused_type.pk])),reverse("settings_compliance"))
        self.assertFalse(DocumentType.objects.filter(pk=self.unused_type.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="document_type.deleted",target_id=str(self.unused_type.pk)).exists())

    def test_a_record_type_with_files_is_retired_not_deleted(self):
        self.file_under(self.filed_type)
        self.client.force_login(self.hr)
        self.client.post(reverse("document_type_remove",args=[self.filed_type.pk]))
        self.filed_type.refresh_from_db(); self.assertFalse(self.filed_type.active)
        # A second press on a retired, still-used type changes nothing and deletes nothing.
        self.client.post(reverse("document_type_remove",args=[self.filed_type.pk]))
        self.assertTrue(DocumentType.objects.filter(pk=self.filed_type.pk).exists())

    def test_a_record_type_a_duty_or_onboarding_step_names_is_kept(self):
        self.duty.document_type=self.unused_type; self.duty.save()
        self.client.force_login(self.owner)
        self.client.post(reverse("document_type_remove",args=[self.unused_type.pk]))
        self.duty.refresh_from_db(); self.assertEqual(self.duty.document_type_id,self.unused_type.pk)
        self.unused_type.refresh_from_db(); self.assertFalse(self.unused_type.active)
        spare=DocumentType.objects.create(organization=self.org,name="Handbook",code="handbook")
        self.OnboardingItem.objects.create(organization=self.org,name="Read handbook",code="handbook",document_type=spare)
        self.client.post(reverse("document_type_remove",args=[spare.pk]))
        self.assertTrue(DocumentType.objects.filter(pk=spare.pk).exists())

    def test_a_field_with_only_blank_answers_is_deleted(self):
        self.PersonCustomValue.objects.create(organization=self.org,person=self.person,definition=self.field,value=None)
        self.PersonCustomValue.objects.create(organization=self.org,person=self.person,definition=self.flag,value=False)
        self.client.force_login(self.hr)
        self.client.post(reverse("custom_field_remove",args=[self.field.pk]))
        self.client.post(reverse("custom_field_remove",args=[self.flag.pk]))
        self.assertFalse(self.CustomFieldDefinition.objects.filter(pk__in=[self.field.pk,self.flag.pk]).exists())
        self.assertFalse(self.PersonCustomValue.objects.filter(person=self.person).exists())

    def test_a_filled_field_is_retired_and_keeps_its_answers(self):
        self.PersonCustomValue.objects.create(organization=self.org,person=self.person,definition=self.field,value="B-12")
        self.client.force_login(self.hr)
        self.client.post(reverse("custom_field_remove",args=[self.field.pk]))
        self.field.refresh_from_db(); self.assertFalse(self.field.active)
        self.assertEqual(self.PersonCustomValue.objects.get(definition=self.field).value,"B-12")

    def test_an_owner_deletes_a_duty_and_its_history_survives(self):
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.post(reverse("compliance_rule_remove",args=[self.duty.pk])),reverse("settings_compliance"))
        self.assertFalse(ComplianceRule.objects.filter(pk=self.duty.pk).exists())
        self.assertTrue(self.RuleRevision.objects.filter(kind=self.RuleRevision.Kind.COMPLIANCE_RULE,rule_id=str(self.duty.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action="compliance_rule.deleted",target_id=str(self.duty.pk)).exists())

    def test_hr_cannot_delete_a_duty(self):
        self.client.force_login(self.hr)
        self.assertEqual(self.client.post(reverse("compliance_rule_remove",args=[self.duty.pk])).status_code,403)
        self.assertTrue(ComplianceRule.objects.filter(pk=self.duty.pk).exists())

    def test_removal_is_post_only_and_tenant_bound(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("document_type_remove",args=[self.unused_type.pk])).status_code,405)
        foreign=DocumentType.objects.create(organization=self.other,name="Foreign",code="foreign")
        self.assertEqual(self.client.post(reverse("document_type_remove",args=[foreign.pk])).status_code,404)
        self.assertTrue(DocumentType.objects.filter(pk=foreign.pk).exists())

    def test_settings_page_offers_delete_or_retire_by_usage(self):
        self.file_under(self.filed_type)
        self.PersonCustomValue.objects.create(organization=self.org,person=self.person,definition=self.field,value="B-12")
        self.client.force_login(self.owner)
        page=self.client.get(reverse("settings_compliance"))
        self.assertContains(page,reverse("document_type_remove",args=[self.unused_type.pk]))
        self.assertContains(page,reverse("compliance_rule_remove",args=[self.duty.pk]))
        self.assertContains(page,"1 filled")
        self.assertContains(page,'">Retire</button>',count=2)

    def test_catalog_usage_filters_leave_the_control_matrix_alone(self):
        self.file_under(self.filed_type)
        self.PersonCustomValue.objects.create(organization=self.org,person=self.person,definition=self.field,value="B-12")
        self.client.force_login(self.owner)
        for usage, types, fields in (
            ("unused", {self.unused_type.pk}, {self.flag.pk}),
            ("used", {self.filed_type.pk}, {self.field.pk}),
            ("all", {self.unused_type.pk,self.filed_type.pk}, {self.field.pk,self.flag.pk}),
        ):
            with self.subTest(usage=usage):
                page=self.client.get(reverse("settings_compliance"),{"usage":usage})
                self.assertEqual({item.pk for item in page.context["document_types"]},types)
                self.assertEqual({item.pk for item in page.context["field_definitions"]},fields)
                self.assertContains(page,self.duty.name)
        page=self.client.get(reverse("settings_compliance"),{"usage":"invalid"})
        self.assertEqual(page.context["usage"],"all")
        self.assertContains(page,"Choose a supported catalog usage filter.")


class CredentialRenewalTest(TestCase):
    """A renewed licence updates the record it renews instead of duplicating it."""

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Credential, CredentialType
        self.Credential=Credential
        User=get_user_model(); self.owner=User.objects.create_user(username="renew@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Renew LLC",display_name="Renew",slug="renew")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER); self.client.force_login(self.owner)
        self.person=Person.objects.create(organization=self.org,first_name="Dana",last_name="Wolfe",status=Person.Status.ACTIVE,is_commissioned_officer=True)
        self.kind=CredentialType.objects.create(organization=self.org,name="Commissioned security officer",code="commission",applies_to=["commissioned"],warning_days=30,reminder_days_before=[90,60])
        self.credential=Credential.objects.create(organization=self.org,person=self.person,credential_type=self.kind,status=Credential.Status.ACTIVE,number="C-400",expires_on=timezone.localdate()+timedelta(days=12))

    def test_renewal_keeps_one_record_and_records_the_change(self):
        from datetime import timedelta
        from django.utils import timezone
        original=self.credential.expires_on
        later=original+timedelta(days=720)
        response=self.client.post(reverse("credential_edit",args=[self.credential.pk]),{"credential_type":self.kind.pk,"status":self.Credential.Status.ACTIVE,"number":"C-400","issued_on":"","expires_on":later,"notes":"renewed via TOPS"})
        self.assertRedirects(response,reverse("person_detail",args=[self.person.pk])+"?tab=credentials")
        self.credential.refresh_from_db()
        self.assertEqual(self.credential.expires_on,later)
        self.assertEqual(self.person.credentials.count(),1)
        event=AuditEvent.objects.get(action="credential.updated")
        self.assertEqual(event.metadata["changes"]["expires_on"]["before"],str(original))
        self.assertEqual(event.metadata["changes"]["expires_on"]["after"],str(later))
    def test_the_ladder_re_arms_for_a_new_expiry_date(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Notification
        from .services import queue_compliance_reminders
        self.assertEqual(queue_compliance_reminders(),2)   # inside the 90-day lead, in-app + email
        self.assertEqual(queue_compliance_reminders(),0)   # not again for the same level
        expires=timezone.localdate()+timedelta(days=45)
        self.client.post(reverse("credential_edit",args=[self.credential.pk]),{"credential_type":self.kind.pk,"status":self.Credential.Status.ACTIVE,"number":"C-400","expires_on":expires})
        self.assertEqual(queue_compliance_reminders(),2)   # the renewed date opens a fresh ladder
        self.assertEqual(Notification.objects.filter(event_type="credential.reminder").count(),4)

    def test_a_number_already_on_file_for_that_person_is_refused(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Credential
        Credential.objects.create(organization=self.org,person=self.person,credential_type=self.kind,status=Credential.Status.ACTIVE,number="C-500",expires_on=timezone.localdate()+timedelta(days=200))
        response=self.client.post(reverse("credential_edit",args=[self.credential.pk]),{"credential_type":self.kind.pk,"status":self.Credential.Status.ACTIVE,"number":"C-500","expires_on":self.credential.expires_on})
        self.assertEqual(response.status_code,200)
        self.assertIn("number",response.context["form"].errors)
        self.credential.refresh_from_db(); self.assertEqual(self.credential.number,"C-400")


class ReminderLadderTest(TestCase):
    def setUp(self):
        self.org=Organization.objects.create(legal_name="Ladder LLC",display_name="Ladder",slug="ladder")

    def test_the_level_is_the_smallest_lead_the_date_has_entered(self):
        from .models import CredentialType
        from .services import reminder_level
        kind=CredentialType(organization=self.org,name="x",code="x",warning_days=30,reminder_days_before=[90,60])
        self.assertEqual(kind.reminder_levels,[90,60,30])
        self.assertEqual(reminder_level(kind,95),None)   # outside every configured lead
        self.assertEqual(reminder_level(kind,61),90)     # still the 90-day rung, and deduplicated
        self.assertEqual(reminder_level(kind,59),60)     # the next rung fires when it is entered
        self.assertEqual(reminder_level(kind,29),30)
        self.assertEqual(reminder_level(kind,0),30)      # expiry day is the last rung

    def test_a_missing_obligation_is_queued_once_per_month(self):
        from datetime import date, timedelta
        from .models import CredentialType, Notification
        from .services import queue_compliance_reminders
        User=get_user_model(); owner=User.objects.create_user(username="ladder@example.com",password="correct horse battery staple")
        Membership.objects.create(user=owner,organization=self.org,role=Membership.Role.OWNER)
        Person.objects.create(organization=self.org,first_name="Iris",last_name="Unlicensed",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        CredentialType.objects.create(organization=self.org,name="Noncommissioned officer registration",code="nco",applies_to=["unarmed"])
        today=date(2026,3,2)
        self.assertEqual(queue_compliance_reminders(today),2)                        # in-app + email
        self.assertEqual(queue_compliance_reminders(today+timedelta(days=1)),0)      # not re-nagged daily
        self.assertEqual(queue_compliance_reminders(today+timedelta(days=40)),2)     # next month, again
        self.assertTrue(Notification.objects.filter(event_type="credential.missing").exists())

class WorkforceAcknowledgmentTest(TestCase):
    """One signature must not mark a company record acknowledged for everybody."""

    def setUp(self):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from .models import DocumentType
        User=get_user_model()
        self.owner=User.objects.create_user(username="ack-owner@example.com",password="correct horse battery staple")
        self.first=User.objects.create_user(username="ack-one@example.com",password="correct horse battery staple")
        self.second=User.objects.create_user(username="ack-two@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Ack LLC",display_name="Ack",slug="ack")
        for user,role in ((self.owner,Membership.Role.OWNER),(self.first,Membership.Role.OFFICER),(self.second,Membership.Role.OFFICER)):
            Membership.objects.create(user=user,organization=self.org,role=role)
        self.person_one=Person.objects.create(organization=self.org,user=self.first,first_name="One",last_name="Guard",status=Person.Status.ACTIVE)
        self.person_two=Person.objects.create(organization=self.org,user=self.second,first_name="Two",last_name="Guard",status=Person.Status.ACTIVE)
        self.handbook=DocumentType.objects.create(organization=self.org,name="Employee handbook",code="handbook",audience=DocumentType.Audience.WORKFORCE,acknowledgment_required=True)
        self.licence=DocumentType.objects.create(organization=self.org,name="Class B licence",code="class-b",audience=DocumentType.Audience.MANAGEMENT)
        media=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        override=override_settings(MEDIA_ROOT=media.name); override.enable()
        self.addCleanup(override.disable); self.addCleanup(media.cleanup)
        self.client.force_login(self.owner)
        self.client.post(reverse("document_upload"),{"document_type":self.handbook.pk,"file":SimpleUploadedFile("handbook.pdf",b"%PDF-1.4\nhandbook",content_type="application/pdf")})
        self.client.post(reverse("document_upload"),{"document_type":self.licence.pk,"file":SimpleUploadedFile("licence.pdf",b"%PDF-1.4\nlicence",content_type="application/pdf")})
        from .models import PersonDocument
        self.handbook_document=PersonDocument.objects.get(document_type=self.handbook)
        self.licence_document=PersonDocument.objects.get(document_type=self.licence)

    def acknowledge(self,user,document):
        self.client.force_login(user)
        return self.client.post(reverse("document_acknowledge",args=[document.pk]),{"confirm":"on","signature_name":user.username})

    def test_a_second_worker_still_has_to_sign_after_the_first(self):
        self.acknowledge(self.first,self.handbook_document)
        self.handbook_document.refresh_from_db()
        self.assertIsNone(self.handbook_document.acknowledged_at)   # no single signer means no row-level completion
        self.client.force_login(self.second)
        page=self.client.get(reverse("my_documents"))
        self.assertContains(page,"Review")
        self.assertNotContains(page,"Acknowledged")

    def test_the_roster_lists_who_has_not_signed(self):
        self.acknowledge(self.first,self.handbook_document)
        self.client.force_login(self.owner)
        page=self.client.get(reverse("document_acknowledgments",args=[self.handbook_document.pk]))
        self.assertContains(page,"One Guard")
        self.assertContains(page,"Two Guard")
        self.assertContains(page,"1 acknowledged")
        self.assertContains(page,"1 outstanding")

    def test_reminders_reach_only_the_outstanding_workers(self):
        from .models import Notification
        self.acknowledge(self.first,self.handbook_document)
        self.client.force_login(self.owner)
        self.client.post(reverse("document_remind",args=[self.handbook_document.pk]))
        recipients=set(Notification.objects.filter(event_type="document.acknowledgment_requested").values_list("recipient_id",flat=True))
        self.assertEqual(recipients,{self.second.pk})

    def test_a_worker_cannot_acknowledge_a_management_only_company_record(self):
        response=self.acknowledge(self.second,self.licence_document)
        self.assertEqual(response.status_code,404)
        from .models import DocumentAcknowledgment
        self.assertFalse(DocumentAcknowledgment.objects.filter(document=self.licence_document).exists())

    def test_the_compliance_queue_counts_missing_signatures(self):
        self.acknowledge(self.first,self.handbook_document)
        self.client.force_login(self.owner)
        page=self.client.get(reverse("compliance")+"?kind=documents&show=all")
        self.assertContains(page,"1 of 2 have not acknowledged")


class CheckpointAttributionTest(TestCase):
    """A patrol scan has to name the point that was reached, or it is not evidence."""

    def setUp(self):
        from datetime import timedelta
        from decimal import Decimal
        from django.utils import timezone
        User=get_user_model(); self.owner=User.objects.create_user(username="patrol@example.com",password="correct horse battery staple")
        self.officer=User.objects.create_user(username="patrol-guard@example.com",email="patrol-guard@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Patrol LLC",display_name="Patrol",slug="patrol")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.officer,organization=self.org,role=Membership.Role.OFFICER)
        self.person=Person.objects.create(organization=self.org,user=self.officer,first_name="Pat",last_name="Roll",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        account=Client.objects.create(organization=self.org,name="Plantiff Industries")
        self.site=Site.objects.create(organization=self.org,client=account,name="Gate 3",address="1 Industrial Way",latitude=Decimal("32.776700"),longitude=Decimal("-96.797000"),geofence_radius_meters=400)
        self.other_site=Site.objects.create(organization=self.org,client=account,name="Gate 9",address="9 Other Way",latitude=Decimal("32.900000"),longitude=Decimal("-96.900000"),geofence_radius_meters=400)
        self.now=timezone.now()
        from .models import Shift
        self.shift=Shift.objects.create(organization=self.org,site=self.site,officer=self.person,starts_at=self.now-timedelta(hours=1),ends_at=self.now+timedelta(hours=5),status=Shift.Status.PUBLISHED)
        self.client.force_login(self.owner)
        created=self.client.post(reverse("checkpoint_create",args=[self.site.pk]),{"name":"Loading dock","latitude":Decimal("32.776700"),"longitude":Decimal("-96.797000"),"radius_meters":50,"active":True})
        self.assertRedirects(created,reverse("locations"))
        from .models import Checkpoint
        self.checkpoint=Checkpoint.objects.get(site=self.site)
        self.other_point=Checkpoint.objects.create(organization=self.org,site=self.other_site,name="Far gate",latitude=Decimal("32.900000"),longitude=Decimal("-96.900000"),radius_meters=50)

    def punch(self,**overrides):
        import json,uuid
        from decimal import Decimal
        from django.utils import timezone
        payload={"client_event_id":overrides.get("client_event_id",str(uuid.uuid4())),"kind":overrides.get("kind","checkpoint"),"occurred_at":overrides.get("occurred_at",timezone.now().isoformat()),"shift_id":overrides.get("shift_id",str(self.shift.pk)),"checkpoint_code":overrides.get("checkpoint_code",str(self.checkpoint.scan_code)),"latitude":overrides.get("latitude",str(Decimal("32.776700"))),"longitude":overrides.get("longitude",str(Decimal("-96.797000"))),"offline":False}
        self.client.force_login(self.officer)
        return self.client.post(reverse("punch_api"),data=json.dumps(payload),content_type="application/json")

    def test_a_scan_is_attributed_to_the_named_patrol_point(self):
        response=self.punch()
        self.assertEqual(response.status_code,201)
        from .models import Punch
        punch=Punch.objects.get(pk=response.json()["id"])
        self.assertEqual(punch.checkpoint_id,self.checkpoint.pk)

    def test_an_unattributed_or_fabricated_scan_is_refused(self):
        self.assertEqual(self.punch(checkpoint_code=None).status_code,400)          # no code: unattributed
        self.assertEqual(self.punch(checkpoint_code="00000000-0000-0000-0000-000000000000").status_code,400)
        self.assertEqual(self.punch(checkpoint_code=str(self.other_point.scan_code)).status_code,400)  # another site's point

    def test_a_scan_code_cannot_be_attached_to_an_ordinary_punch(self):
        response=self.punch(kind="in")
        self.assertEqual(response.status_code,400)
        self.assertIn("checkpoint punch",response.json()["error"])

    def test_a_scan_outside_the_points_own_geofence_is_refused(self):
        response=self.punch(latitude="32.950000",longitude="-96.950000")
        self.assertEqual(response.status_code,400)
        self.assertIn("geofence",response.json()["error"])

    def test_the_clock_offers_only_points_that_exist_for_the_officers_sites(self):
        self.client.force_login(self.officer)
        page=self.client.get(reverse("clock"))
        self.assertContains(page,"Loading dock")
        self.assertContains(page,"data-checkpoint")


class OpenPostClaimTest(TestCase):
    """A published post may be unfilled — that is how coverage gets announced.

    The gate belongs at the moment someone is assigned: the officer who asks must qualify,
    and the dispatcher who approves must be refused if the credential lapsed in between.
    """

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User=get_user_model()
        self.owner=User.objects.create_user(username="open-owner@example.com",password="correct horse battery staple")
        self.qualified=User.objects.create_user(username="open-ready@example.com",password="correct horse battery staple")
        self.unqualified=User.objects.create_user(username="open-lapsed@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Open LLC",display_name="Open",slug="open")
        for user,role in ((self.owner,Membership.Role.OWNER),(self.qualified,Membership.Role.OFFICER),(self.unqualified,Membership.Role.OFFICER)):
            Membership.objects.create(user=user,organization=self.org,role=role)
        self.ready=Person.objects.create(organization=self.org,user=self.qualified,first_name="Quinn",last_name="Ready",status=Person.Status.ACTIVE,is_unarmed_officer=True,is_commissioned_officer=True)
        self.lapsed=Person.objects.create(organization=self.org,user=self.unqualified,first_name="Lou",last_name="Lapsed",status=Person.Status.ACTIVE,is_unarmed_officer=True,is_commissioned_officer=True)
        account=Client.objects.create(organization=self.org,name="Nightshift Retail")
        self.site=Site.objects.create(organization=self.org,client=account,name="Store 12",address="12 Main St")
        self.armed=CredentialType.objects.create(organization=self.org,name="Commissioned officer",code="commission",applies_to=["commissioned"],blocks_scheduling=True)
        Credential.objects.create(organization=self.org,person=self.ready,credential_type=self.armed,status=Credential.Status.ACTIVE,number="C-1",expires_on=timezone.localdate()+timedelta(days=200))
        Credential.objects.create(organization=self.org,person=self.lapsed,credential_type=self.armed,status=Credential.Status.ACTIVE,number="C-2",expires_on=timezone.localdate()-timedelta(days=1))
        now=timezone.now()
        self.open_shift=Shift.objects.create(organization=self.org,site=self.site,starts_at=now+timedelta(days=1),ends_at=now+timedelta(days=1,hours=8),status=Shift.Status.PUBLISHED,post_name="Closed-circuit monitor")
        self.guarded=Shift.objects.create(organization=self.org,site=self.site,starts_at=now+timedelta(days=2),ends_at=now+timedelta(days=2,hours=8),status=Shift.Status.PUBLISHED,post_name="Armed lobby")
        self.guarded.required_credentials.add(self.armed)

    def publish(self,post_name,required=()):
        from datetime import timedelta
        from django.utils import timezone
        self.client.force_login(self.owner)
        now=timezone.now()+timedelta(days=3)
        return self.client.post(reverse("shift_create"),{"site":self.site.pk,"officer":"","starts_at":now.strftime("%Y-%m-%dT%H:%M"),"ends_at":(now+timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M"),"status":Shift.Status.PUBLISHED,"post_name":post_name,"required_credentials":[str(item.pk) for item in required]})

    def test_an_unfilled_post_can_be_published_and_reaches_only_the_officers_who_qualify(self):
        from .models import Notification
        response=self.publish("Gate",required=[self.armed])
        self.assertRedirects(response,reverse("schedule"))
        shift=Shift.objects.get(post_name="Gate")
        self.assertIsNone(shift.officer_id)
        self.assertEqual(shift.status,Shift.Status.PUBLISHED)
        recipients=set(Notification.objects.filter(event_type="shift.open").values_list("recipient_id",flat=True))
        self.assertEqual(recipients,{self.qualified.pk})   # the lapsed officer is not invited to ask

    def test_an_officer_requests_a_post_and_the_dispatcher_fills_it(self):
        from .models import Notification, ShiftClaim
        self.client.force_login(self.qualified)
        page=self.client.get(reverse("open_posts"))
        self.assertContains(page,"Closed-circuit monitor")
        self.client.post(reverse("shift_claim"),{"shift_id":self.open_shift.pk,"note":"can cover"})
        claim=ShiftClaim.objects.get(shift=self.open_shift,officer=self.ready)
        self.assertEqual(claim.status,ShiftClaim.Status.REQUESTED)
        self.assertIsNone(claim.shift.officer_id)   # a request alone must not fill the post
        self.client.force_login(self.owner)
        requests_page=self.client.get(reverse("shift_requests",args=[self.open_shift.pk]))
        self.assertContains(requests_page,"Quinn Ready")
        self.client.post(reverse("shift_claim_decide",args=[claim.pk]),{"action":ShiftClaim.Status.APPROVED})
        self.open_shift.refresh_from_db(); claim.refresh_from_db()
        self.assertEqual(self.open_shift.officer_id,self.ready.pk)
        self.assertEqual(self.open_shift.status,Shift.Status.PUBLISHED)
        self.assertEqual(claim.status,ShiftClaim.Status.APPROVED)
        self.assertTrue(Notification.objects.filter(recipient=self.qualified,event_type="shift.claim_approved").exists())
        self.assertTrue(AuditEvent.objects.filter(action="shift.claim_approved").exists())

    def test_a_post_the_officer_does_not_qualify_for_is_not_theirs_to_request(self):
        from .models import ShiftClaim
        self.client.force_login(self.unqualified)
        page=self.client.get(reverse("open_posts"))
        self.assertContains(page,"expired")            # the reason is shown, not a silent omission
        response=self.client.post(reverse("shift_claim"),{"shift_id":self.guarded.pk})
        self.assertRedirects(response,reverse("open_posts"))
        self.assertFalse(ShiftClaim.objects.exists())

    def test_approval_is_refused_when_the_credential_lapsed_after_the_request(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import ShiftClaim
        self.client.force_login(self.qualified)
        self.client.post(reverse("shift_claim"),{"shift_id":self.guarded.pk})
        claim=ShiftClaim.objects.get(shift=self.guarded,officer=self.ready)
        Credential.objects.filter(person=self.ready).update(expires_on=timezone.localdate()-timedelta(days=1))
        self.client.force_login(self.owner)
        page=self.client.get(reverse("shift_requests",args=[self.guarded.pk]))
        self.assertContains(page,"expired")
        self.client.post(reverse("shift_claim_decide",args=[claim.pk]),{"action":ShiftClaim.Status.APPROVED})
        self.guarded.refresh_from_db(); claim.refresh_from_db()
        self.assertIsNone(self.guarded.officer_id)
        self.assertEqual(claim.status,ShiftClaim.Status.REQUESTED)

    def test_one_request_per_officer_per_post_and_a_request_can_be_withdrawn(self):
        from .models import ShiftClaim
        self.client.force_login(self.qualified)
        self.client.post(reverse("shift_claim"),{"shift_id":self.open_shift.pk})
        self.client.post(reverse("shift_claim"),{"shift_id":self.open_shift.pk})
        self.assertEqual(ShiftClaim.objects.filter(shift=self.open_shift).count(),1)
        claim=ShiftClaim.objects.get(shift=self.open_shift)
        self.client.post(reverse("shift_claim_withdraw",args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status,ShiftClaim.Status.WITHDRAWN)
        self.client.post(reverse("shift_claim"),{"shift_id":self.open_shift.pk})
        self.assertEqual(ShiftClaim.objects.filter(shift=self.open_shift,status=ShiftClaim.Status.REQUESTED).count(),1)

    def test_assigning_an_officer_who_does_not_qualify_is_still_refused(self):
        from datetime import timedelta
        from django.utils import timezone
        self.client.force_login(self.owner)
        now=timezone.now()+timedelta(days=4)
        response=self.client.post(reverse("shift_create"),{"site":self.site.pk,"officer":self.lapsed.pk,"starts_at":now.strftime("%Y-%m-%dT%H:%M"),"ends_at":(now+timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M"),"status":Shift.Status.PUBLISHED,"post_name":"Assigned while lapsed","required_credentials":[str(self.armed.pk)]})
        self.assertEqual(response.status_code,200)
        self.assertFalse(Shift.objects.filter(post_name="Assigned while lapsed").exists())
        self.assertIn("expired",str(response.context["form"].errors))


class OperableConfigurationTest(TestCase):
    """The catalogs stopped being create-only, and the dashboard stopped being decorative.

    A wrong geofence, a requirement whose applicability changed, or a rule that must be
    approved after the fact were all admin-database edits, invisible to the people who own
    the record.
    """

    def setUp(self):
        from datetime import datetime, time, timedelta
        from django.utils import timezone
        from .services import schedule_week_start
        User=get_user_model()
        self.owner=User.objects.create_user(username="config@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Config LLC",display_name="Config",slug="config")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER); self.client.force_login(self.owner)
        self.branch=Branch.objects.create(organization=self.org,name="Arlington")
        self.client_account=Client.objects.create(organization=self.org,name="Vault Corp")
        self.site=Site.objects.create(organization=self.org,client=self.client_account,name="Vault",address="1 Safe St",geofence_radius_meters=200)
        self.rule=CredentialType.objects.create(organization=self.org,name="Guard registration",code="guard-reg",applies_to=["unarmed"],warning_days=30)
        self.record_type=DocumentType.objects.create(organization=self.org,name="Screening",code="screening")
        self.field=CustomFieldDefinition.objects.create(organization=self.org,name="TOPS receipt",key="tops-receipt",kind="text")
        self.person=Person.objects.create(organization=self.org,first_name="Ada",last_name="Bris",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        now=timezone.now()
        # Mid-week rather than `now + 2 hours`: the schedule window is the current Monday-to-Sunday
        # week, so a fixture placed against the run clock leaves it when the suite runs late on a
        # Sunday. The far-future post below is meant to fall outside that window, this one may not.
        week_start=schedule_week_start()
        this_week_start=timezone.make_aware(datetime.combine(week_start+timedelta(days=1),time(8,0)))
        self.this_week=Shift.objects.create(organization=self.org,site=self.site,officer=self.person,
            starts_at=this_week_start,ends_at=this_week_start+timedelta(hours=8),status=Shift.Status.PUBLISHED)
        self.next_year=Shift.objects.create(organization=self.org,site=self.site,starts_at=now+timedelta(days=400),ends_at=now+timedelta(days=400,hours=8),status=Shift.Status.DRAFT,post_name="Far future draft")

    def test_a_site_can_be_corrected_and_closed(self):
        response=self.client.post(reverse("site_edit",args=[self.site.pk]),{"client":self.client_account.pk,"branch":"","name":"Vault","address":"2 Safer St","latitude":"","longitude":"","geofence_radius_meters":120})
        self.assertRedirects(response,reverse("locations"))
        self.site.refresh_from_db()
        self.assertEqual(self.site.geofence_radius_meters,120)
        self.assertFalse(self.site.active)   # an unchecked box closes the post
        self.assertTrue(AuditEvent.objects.filter(action="site.updated",target_id=str(self.site.pk)).exists())

    def test_a_requirement_can_be_approved_after_it_was_created(self):
        """Approval used to be reachable only on the create form, so a drafted rule stayed
        unapproved and unenforceable unless it was deleted and re-typed."""
        self.assertFalse(self.rule.is_approved)
        response=self.client.post(reverse("credential_type_edit",args=[self.rule.pk]),{"name":"Guard registration","code":"guard-reg","jurisdiction":"Texas","authority_url":"https://www.dps.texas.gov/section/private-security","authority_reference":"Occ. Code 1702.222","interpretation":"Unarmed officers must hold current registration.","effective_from":"","effective_until":"","blocks_scheduling":"on","blocks_clock_in":"on","warning_days":30,"reminder_days_before":"90, 60","evidence_required":"on","applies_to":["unarmed"],"active":"on","approve":"yes"})
        self.assertRedirects(response,reverse("settings_compliance"))
        self.rule.refresh_from_db()
        self.assertTrue(self.rule.is_approved)
        self.assertEqual(self.rule.approved_by_id,self.owner.pk)
        self.assertEqual(self.rule.reminder_days_before,[90,60])
        self.assertEqual(self.rule.reminder_levels,[90,60,30])

    def test_a_rejected_source_leaves_the_rule_draft(self):
        self.client.post(reverse("credential_type_edit",args=[self.rule.pk]),{"name":"Guard registration","code":"guard-reg","jurisdiction":"Texas","authority_url":"","authority_reference":"","interpretation":"","warning_days":30,"reminder_days_before":"","applies_to":["unarmed"],"active":"on"})
        self.rule.refresh_from_db()
        self.assertFalse(self.rule.is_approved)

    def test_branch_and_record_catalogs_are_editable(self):
        self.assertRedirects(self.client.post(reverse("branch_edit",args=[self.branch.pk]),{"name":"Arlington","city":"Arlington","active":"on"}),reverse("branches"))
        self.assertRedirects(self.client.post(reverse("document_type_edit",args=[self.record_type.pk]),{"name":"Background screening","code":"screening","audience":DocumentType.Audience.PERSON,"sensitivity":DocumentType.Sensitivity.RESTRICTED.value,"retention_days":"730","acknowledgment_required":"on","signature_required":"","active":"on"}),reverse("settings_compliance"))
        self.record_type.refresh_from_db()
        self.assertEqual(self.record_type.retention_days,730)
        self.assertTrue(self.record_type.acknowledgment_required)
        # A screening result is family-4 evidence, so this also proves the secrecy rung travels
        # through the shared catalog editor rather than being display-only.
        self.assertEqual(self.record_type.sensitivity,DocumentType.Sensitivity.RESTRICTED)
        self.assertRedirects(self.client.post(reverse("custom_field_edit",args=[self.field.pk]),{"name":"TOPS receipt","key":"tops-receipt","kind":"text","required":"on","sensitive":"","active":"on"}),reverse("settings_compliance"))

    def test_the_schedule_shows_one_week_at_a_time(self):
        page=self.client.get(reverse("schedule"))
        self.assertContains(page,"Vault")
        self.assertNotContains(page,"Far future draft")   # outside the week window
        self.assertContains(page,"Open posts")
        next_week=self.client.get(reverse("schedule")+"?week=-52")
        self.assertContains(next_week,"Previous week")

    def test_cancelling_a_post_needs_a_reason_and_never_erases_time_evidence(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Punch
        response=self.client.post(reverse("shift_cancel",args=[self.this_week.pk]),{"reason":"x"})
        self.assertRedirects(response,reverse("schedule"))
        self.this_week.refresh_from_db()
        self.assertEqual(self.this_week.status,Shift.Status.PUBLISHED)
        Punch.objects.create(client_event_id="b7a3c1f4-33c4-4f3a-9c4f-4d9f4d9f4d9f",organization=self.org,person=self.person,shift=self.this_week,kind=Punch.Kind.IN,occurred_at=timezone.now())
        self.client.post(reverse("shift_cancel",args=[self.this_week.pk]),{"reason":"Officer is unavailable and no relief was found"})
        self.this_week.refresh_from_db()
        self.assertEqual(self.this_week.status,Shift.Status.PUBLISHED)   # punches exist
        self.assertEqual(Punch.objects.filter(shift=self.this_week).count(),1)

    def test_the_dashboard_reports_the_numbers_it_used_to_leave_blank(self):
        page=self.client.get(reverse("dashboard"))
        self.assertContains(page,"Assignment readiness")
        self.assertContains(page,"Coverage in the next 7 days")
        self.assertIn("compliance_attention",page.context)
        self.assertNotContains(page,"Control matrix pending")
        self.assertNotContains(page,"Coming next")


class PostRequirementsAndRateTest(TestCase):
    """The contract and the site impose requirements and rates; the shift may only narrow them.

    Two product rulings are encoded here: scheduling is blocked by the credentials the post
    actually requires (widened to whatever the client or site adds), and hours are paid and billed
    at the rate that governs the post they were worked on — a guard split across two accounts
    cannot be settled at one blended number.
    """

    def setUp(self):
        from datetime import datetime, timedelta
        from decimal import Decimal
        from zoneinfo import ZoneInfo
        User=get_user_model()
        self.owner=User.objects.create_user(username="rates@example.com",password="correct horse battery staple")
        self.officer=User.objects.create_user(username="rates-guard@example.com",email="rates-guard@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Rates LLC",display_name="Rates",slug="rates")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.officer,organization=self.org,role=Membership.Role.OFFICER)
        self.person=Person.objects.create(organization=self.org,user=self.officer,first_name="Ray",last_name="Torrent",status=Person.Status.ACTIVE,is_unarmed_officer=True,hourly_rate=Decimal("18.00"))
        self.site_only=CredentialType.objects.create(organization=self.org,name="Site orientation",code="orientation",applies_to=["unarmed"],blocks_scheduling=True)
        self.registration=CredentialType.objects.create(organization=self.org,name="Guard registration",code="reg",applies_to=["unarmed"],blocks_scheduling=True)
        self.client_account=Client.objects.create(organization=self.org,name="Meridian Plaza",default_bill_rate=Decimal("42.00"))
        self.client_account.required_credentials.add(self.registration)
        self.desk=Site.objects.create(organization=self.org,client=self.client_account,name="Desk 1",address="1 Plaza",default_pay_rate=Decimal("25.00"))
        self.desk.required_credentials.add(self.site_only)
        self.warehouse=Site.objects.create(organization=self.org,client=self.client_account,name="Warehouse",address="2 Plaza")
        TimePolicy.objects.create(organization=self.org,timezone="America/Chicago",workweek_start=0,overtime_after_hours=Decimal("12.00"),rounding_mode=TimePolicy.RoundingMode.EXACT,rounding_minutes=1)
        self.zone=ZoneInfo("America/Chicago")
        self.timedelta=timedelta
        self.datetime=datetime
        self.client.force_login(self.owner)

    def qualify(self,officer,types):
        from datetime import timedelta
        from django.utils import timezone
        for item in types:
            Credential.objects.create(organization=self.org,person=officer,credential_type=item,status=Credential.Status.ACTIVE,expires_on=timezone.localdate()+timedelta(days=300))

    def schedule(self,site,officer,required=(),start_hour=8,hours=10,day=2,pay_rate=None,bill_rate=None):
        starts=self.datetime(2026,2,day,start_hour,0,tzinfo=self.zone)
        return Shift.objects.create(organization=self.org,site=site,officer=officer,starts_at=starts,ends_at=starts+self.timedelta(hours=hours),status=Shift.Status.PUBLISHED,pay_rate=pay_rate,bill_rate=bill_rate)

    def test_a_requirement_set_on_the_contract_blocks_a_post_that_never_mentions_it(self):
        from datetime import timedelta
        from django.utils import timezone
        response=self.client.post(reverse("shift_create"),{"site":self.warehouse.pk,"officer":self.person.pk,"starts_at":"2026-02-02T08:00","ends_at":"2026-02-02T18:00","status":Shift.Status.PUBLISHED,"post_name":"Dock","required_credentials":[]})
        self.assertEqual(response.status_code,200)
        self.assertFalse(Shift.objects.filter(post_name="Dock").exists())
        errors=str(response.context["form"].errors)
        self.assertIn("Guard registration: missing",errors)
        self.assertIn("Required by the contract",errors)

    def test_the_inherited_rule_is_visible_to_the_officer_who_fails_it(self):
        from django.utils import timezone
        starts=timezone.now()+self.timedelta(days=1)   # the open-post list only shows upcoming posts
        Shift.objects.create(organization=self.org,site=self.warehouse,starts_at=starts,ends_at=starts+self.timedelta(hours=8),status=Shift.Status.PUBLISHED,post_name="Dock")
        self.client.force_login(self.officer)
        page=self.client.get(reverse("open_posts"))
        self.assertContains(page,"Dock")
        self.assertContains(page,"Required by the contract")

    def test_a_site_requirement_and_the_officers_own_records_are_both_checked(self):
        from datetime import timedelta
        from django.utils import timezone
        self.qualify(self.person,[self.registration])
        # The site adds orientation on top of the contract's registration.
        response=self.client.post(reverse("shift_create"),{"site":self.desk.pk,"officer":self.person.pk,"starts_at":"2026-02-02T08:00","ends_at":"2026-02-02T18:00","status":Shift.Status.PUBLISHED,"post_name":"Desk cover","required_credentials":[]})
        self.assertEqual(response.status_code,200)
        self.assertIn("Site orientation: missing. Required by the site.",str(response.context["form"].errors))
        self.qualify(self.person,[self.site_only])
        response=self.client.post(reverse("shift_create"),{"site":self.desk.pk,"officer":self.person.pk,"starts_at":"2026-02-02T08:00","ends_at":"2026-02-02T18:00","status":Shift.Status.PUBLISHED,"post_name":"Desk cover","required_credentials":[]})
        self.assertRedirects(response,reverse("schedule"))

    def test_rates_resolve_post_then_site_then_contract_then_officer(self):
        from decimal import Decimal
        from .services import effective_rates
        self.qualify(self.person,[self.registration,self.site_only])
        at_site=self.schedule(self.desk,self.person)
        rates=effective_rates(at_site)
        self.assertEqual(rates["pay_rate"],Decimal("25.00"))      # the site's rate wins over the officer's
        self.assertEqual(rates["pay_source"],"site")
        self.assertEqual(rates["bill_rate"],Decimal("42.00"))     # inherited from the contract
        self.assertEqual(rates["bill_source"],"contract")
        self.warehouse.default_pay_rate=Decimal("21.50");self.warehouse.save()
        overridden=self.schedule(self.warehouse,self.person,pay_rate=Decimal("30.00"),day=3)
        rates=effective_rates(overridden)
        self.assertEqual(rates["pay_rate"],Decimal("30.00"))      # one-off premium for this post only
        self.assertEqual(rates["pay_source"],"post")
        fallback=self.schedule(self.warehouse,self.person,day=4)
        self.warehouse.default_pay_rate=None;self.warehouse.save()
        rates=effective_rates(fallback)
        self.assertEqual(rates["pay_rate"],Decimal("18.00"))      # finally the officer's own rate
        self.assertEqual(rates["pay_source"],"officer")

    def test_hours_split_across_posts_and_the_workweek_overtime_is_counted_once(self):
        from decimal import Decimal
        from uuid import uuid4
        from .models import Punch
        from .services import payroll_rows
        self.qualify(self.person,[self.registration,self.site_only])
        desk=self.schedule(self.desk,self.person,hours=10,day=2)          # Mon 08:00-18:00 at 25.00
        dock=self.schedule(self.warehouse,self.person,hours=10,day=3,start_hour=8)  # Tue 8h at 18.00
        for shift,(in_hour,out_hour) in ((desk,(8,18)),(dock,(8,18))):
            starts=shift.starts_at
            Punch.objects.create(organization=self.org,person=self.person,shift=shift,client_event_id=uuid4(),kind=Punch.Kind.IN,occurred_at=starts)
            Punch.objects.create(organization=self.org,person=self.person,shift=shift,client_event_id=uuid4(),kind=Punch.Kind.OUT,occurred_at=starts+self.timedelta(hours=out_hour-in_hour))
        rows=payroll_rows(self.org,self.datetime(2026,2,1,tzinfo=self.zone),self.datetime(2026,2,8,tzinfo=self.zone))
        by_site={row["site"]:row for row in rows}
        self.assertEqual(len(rows),2)
        # Overtime threshold is 12 h for the week, not per post: the first 10 h at the desk are
        # all regular, then the dock's hours carry the whole 10 h of premium.
        self.assertEqual(by_site["Desk 1"]["regular_hours"],Decimal("10.00"))
        self.assertEqual(by_site["Desk 1"]["overtime_hours"],Decimal("0.00"))
        self.assertEqual(by_site["Desk 1"]["estimated_pay"],Decimal("250.00"))
        self.assertEqual(by_site["Warehouse"]["regular_hours"],Decimal("2.00"))
        self.assertEqual(by_site["Warehouse"]["overtime_hours"],Decimal("8.00"))
        self.assertEqual(by_site["Warehouse"]["estimated_bill"],Decimal("420.00"))
        # 2 h at 18.00 plus 8 h at 27.00, billed at 42.00: the spread is what the operator reads.
        self.assertEqual(by_site["Warehouse"]["estimated_pay"],Decimal("252.00"))
        self.assertEqual(by_site["Warehouse"]["margin"],Decimal("168.00"))

    def test_the_export_carries_the_rate_columns(self):
        from .services import PAYROLL_EXPORT_FIELDS
        for field in ("client","site","pay_rate","pay_rate_source","bill_rate","estimated_pay","estimated_bill","margin"):
            self.assertIn(field,PAYROLL_EXPORT_FIELDS)


class ScopedAuthorityTest(TestCase):
    """Bounded manager authority: what one branch supervisor may list, open, and decide.

    The product's discovery decisions require supervisors and dispatchers to act "within their
    assigned scope", and roles on their own are organization-wide. Two things had to be true at
    once for that to be real rather than decorative: a scope must narrow every surface a
    manager touches, and a manager with no scope must keep exactly the authority they have
    today — otherwise the first release of the table locks people out of their own company.
    """

    def setUp(self):
        from datetime import datetime, time, timedelta
        from uuid import uuid4
        from django.utils import timezone
        from .services import schedule_week_start
        User=get_user_model()
        self.owner=User.objects.create_user(username="scope-owner@example.com",password="correct horse battery staple")
        self.bounded=User.objects.create_user(username="scope-austin@example.com",password="correct horse battery staple")
        self.unbound=User.objects.create_user(username="scope-all@example.com",password="correct horse battery staple")
        self.hr=User.objects.create_user(username="scope-hr@example.com",password="correct horse battery staple")
        self.dispatch=User.objects.create_user(username="scope-dispatch@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Scope Security LLC",display_name="Scope",slug="scope-security")
        self.austin=Branch.objects.create(organization=self.org,name="Austin")
        self.dallas=Branch.objects.create(organization=self.org,name="Dallas")
        self.plaza=Client.objects.create(organization=self.org,name="Meridian Plaza")
        self.harbor=Client.objects.create(organization=self.org,name="Harbor Logistics")
        self.tower=Site.objects.create(organization=self.org,client=self.plaza,branch=self.austin,name="Tower",address="1 Plaza")
        self.dock=Site.objects.create(organization=self.org,client=self.harbor,branch=self.dallas,name="Dock",address="2 Harbor")
        self.ana=Person.objects.create(organization=self.org,branch=self.austin,first_name="Ana",last_name="Delgado",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        self.bo=Person.objects.create(organization=self.org,branch=self.dallas,first_name="Bo",last_name="Nakamura",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        # A guard filed to one branch who is stood at another branch's post is the case every
        # axis of the scope model has to answer for, so the fixture keeps one of them.
        self.cy=Person.objects.create(organization=self.org,branch=self.austin,first_name="Cy",last_name="Rivera",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        self.registration=CredentialType.objects.create(organization=self.org,name="Guard registration",code="reg",applies_to=["unarmed"],warning_days=30,blocks_scheduling=True)
        today=timezone.localdate()
        Credential.objects.create(organization=self.org,person=self.ana,credential_type=self.registration,status=Credential.Status.ACTIVE,number="A-1",expires_on=today+timedelta(days=5))
        Credential.objects.create(organization=self.org,person=self.bo,credential_type=self.registration,status=Credential.Status.ACTIVE,number="D-1",expires_on=today+timedelta(days=6))
        now=timezone.now()
        # All three posts sit inside the week the schedule page renders, which is the Monday-to-Sunday
        # window the view computes. Placed at `now + 1/2 days` instead, one of them leaves that window
        # whenever the suite runs late in the week — a Saturday run put "Loan cover" on the following
        # Monday and the scope assertions below then failed for a reason that had nothing to do with
        # scope. `schedule_week_start` is the page's own function, so this cannot drift from it.
        week_start=schedule_week_start()
        def slot(day):
            starts=timezone.make_aware(datetime.combine(week_start+timedelta(days=day),time(8,0)))
            return starts,starts+timedelta(hours=8)
        austin_start,austin_end=slot(1)
        dallas_start,dallas_end=slot(1)
        loan_start,loan_end=slot(2)
        self.austin_post=Shift.objects.create(organization=self.org,site=self.tower,officer=self.ana,starts_at=austin_start,
            ends_at=austin_end,status=Shift.Status.PUBLISHED,post_name="Plaza lobby")
        self.dallas_post=Shift.objects.create(organization=self.org,site=self.dock,officer=self.bo,starts_at=dallas_start,
            ends_at=dallas_end,status=Shift.Status.PUBLISHED,post_name="Harbor gate")
        self.borrowed_post=Shift.objects.create(organization=self.org,site=self.dock,officer=self.cy,starts_at=loan_start,
            ends_at=loan_end,status=Shift.Status.DRAFT,post_name="Loan cover")
        self.austin_punch=Punch.objects.create(organization=self.org,person=self.ana,shift=self.austin_post,client_event_id=uuid4(),kind=Punch.Kind.IN,occurred_at=now,review_status=Punch.Review.PENDING,exception_reason="Punch was outside the site geofence.")
        self.dallas_punch=Punch.objects.create(organization=self.org,person=self.bo,shift=self.dallas_post,client_event_id=uuid4(),kind=Punch.Kind.IN,occurred_at=now,review_status=Punch.Review.PENDING,exception_reason="Punch was outside the site geofence.")
        self.austin_membership=Membership.objects.create(user=self.bounded,organization=self.org,role=Membership.Role.SUPERVISOR)
        self.unbound_membership=Membership.objects.create(user=self.unbound,organization=self.org,role=Membership.Role.SUPERVISOR)
        self.hr_membership=Membership.objects.create(user=self.hr,organization=self.org,role=Membership.Role.HR)
        self.dispatch_membership=Membership.objects.create(user=self.dispatch,organization=self.org,role=Membership.Role.SCHEDULER)
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        AuthorityScope.objects.create(membership=self.austin_membership,organization=self.org,branch=self.austin)
        self.timedelta=timedelta

    def test_a_manager_with_no_scope_still_sees_the_whole_company(self):
        self.client.force_login(self.unbound)
        page=self.client.get(reverse("people"))
        self.assertContains(page,"Ana Delgado")
        self.assertContains(page,"Bo Nakamura")

    def test_a_branch_scope_narrows_the_directory_and_the_profile_route(self):
        self.client.force_login(self.bounded)
        page=self.client.get(reverse("people"))
        self.assertContains(page,"Ana Delgado")
        self.assertContains(page,"Cy Rivera")   # filed to Austin even though stood in Dallas
        self.assertNotContains(page,"Bo Nakamura")
        self.assertContains(page,"Austin branch")            # the screen says what is being shown
        self.assertEqual(self.client.get(reverse("person_detail",args=[self.bo.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse("person_detail",args=[self.ana.pk])).status_code,200)

    def test_a_compliance_queue_outside_the_scope_is_not_the_queue_shown(self):
        self.client.force_login(self.bounded)
        page=self.client.get(reverse("compliance"),{"kind":"credentials","show":"all"})
        self.assertContains(page,"A-1")
        self.assertNotContains(page,"D-1")
        self.client.force_login(self.unbound)
        both=self.client.get(reverse("compliance"),{"kind":"credentials","show":"all"})
        self.assertContains(both,"A-1")
        self.assertContains(both,"D-1")

    def test_editing_a_record_in_another_branch_is_refused(self):
        from django.utils import timezone
        other=Credential.objects.get(person=self.bo,credential_type=self.registration)
        mine=Credential.objects.get(person=self.ana,credential_type=self.registration)
        TrainingRecord.objects.create(organization=self.org,person=self.bo,course_name="Dock safety",completed_on=timezone.localdate())
        TrainingRecord.objects.create(organization=self.org,person=self.ana,course_name="Plaza orientation",completed_on=timezone.localdate())
        self.client.force_login(self.bounded)
        self.assertEqual(self.client.get(reverse("credential_edit",args=[other.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse("credential_edit",args=[mine.pk])).status_code,200)
        register=self.client.get(reverse("training"))
        self.assertContains(register,"Plaza orientation")
        self.assertNotContains(register,"Dock safety")

    def test_the_schedule_covers_the_posts_in_scope_and_the_posts_their_people_stand_elsewhere(self):
        self.client.force_login(self.bounded)
        page=self.client.get(reverse("schedule"))
        self.assertContains(page,"Plaza lobby")     # an Austin site
        self.assertContains(page,"Loan cover")      # an Austin officer, borrowed to a Dallas post
        self.assertNotContains(page,"Harbor gate")  # Dallas post, Dallas officer

    def test_a_timecard_exception_elsewhere_is_not_theirs_to_approve(self):
        self.client.force_login(self.bounded)
        page=self.client.get(reverse("time_review"))
        self.assertContains(page,"Ana Delgado")
        self.assertNotContains(page,"Bo Nakamura")
        refused=self.client.post(reverse("punch_review",args=[self.dallas_punch.pk]),{"action":Punch.Review.ACCEPTED,"reason":"looks fine"})
        self.assertEqual(refused.status_code,404)
        self.dallas_punch.refresh_from_db()
        self.assertEqual(self.dallas_punch.review_status,Punch.Review.PENDING)
        self.client.post(reverse("punch_review",args=[self.austin_punch.pk]),{"action":Punch.Review.ACCEPTED,"reason":"verified on site"})
        self.austin_punch.refresh_from_db()
        self.assertEqual(self.austin_punch.review_status,Punch.Review.ACCEPTED)

    def test_a_scoped_manager_cannot_put_a_post_at_a_site_they_do_not_cover(self):
        from django.utils import timezone
        self.client.force_login(self.bounded)
        starts=timezone.now()+self.timedelta(days=4)
        response=self.client.post(reverse("shift_create"),{"site":self.dock.pk,"officer":"","starts_at":starts.strftime("%Y-%m-%dT%H:%M"),"ends_at":(starts+self.timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M"),"status":Shift.Status.DRAFT,"post_name":"Not mine"})
        self.assertEqual(response.status_code,200)
        self.assertFalse(Shift.objects.filter(post_name="Not mine").exists())
        self.assertIn("not one of the available choices",str(response.context["form"].errors["site"]))

    def test_an_open_post_request_reaches_only_the_managers_who_cover_it(self):
        from .models import ShiftClaim
        from .scope import dispatch_recipients_for_shift
        open_post=Shift.objects.create(organization=self.org,site=self.dock,starts_at=self.austin_post.starts_at+self.timedelta(days=3),ends_at=self.austin_post.starts_at+self.timedelta(days=3,hours=8),status=Shift.Status.PUBLISHED,post_name="Uncovered dock")
        claim=ShiftClaim.objects.create(organization=self.org,shift=open_post,officer=self.bo,status=ShiftClaim.Status.REQUESTED)
        recipients=dispatch_recipients_for_shift(open_post,self.bo)
        self.assertIn(self.owner.pk,recipients)          # a company-level role always hears
        self.assertIn(self.unbound.pk,recipients)        # a manager with no scope covers everything
        self.assertNotIn(self.bounded.pk,recipients)     # Austin does not reach the Dallas post
        self.client.force_login(self.bounded)
        self.assertEqual(self.client.post(reverse("shift_claim_decide",args=[claim.pk]),{"action":ShiftClaim.Status.APPROVED}).status_code,404)
        claim.refresh_from_db(); open_post.refresh_from_db()
        self.assertEqual(claim.status,ShiftClaim.Status.REQUESTED)
        self.assertIsNone(open_post.officer_id)
        self.client.force_login(self.unbound)
        self.client.post(reverse("shift_claim_decide",args=[claim.pk]),{"action":ShiftClaim.Status.APPROVED})
        open_post.refresh_from_db()
        self.assertEqual(open_post.officer_id,self.bo.pk)

    def test_the_overview_does_not_read_the_audit_log_for_a_bounded_manager(self):
        AuditEvent.objects.create(organization=self.org,actor=self.owner,action="payroll.reopened",
            target_type="payroll_run",target_id="period-7",metadata={"reason":"quarter-end dispute"})
        self.client.force_login(self.bounded)
        page=self.client.get(reverse("dashboard"))
        self.assertNotContains(page,"payroll.reopened")   # /audit/ answers 403 here; the tiles must not leak it
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("dashboard")),"payroll.reopened")

    def test_a_company_level_role_is_never_narrowed_by_a_grant(self):
        AuthorityScope.objects.create(membership=self.hr_membership,organization=self.org,branch=self.austin)
        self.client.force_login(self.hr)
        page=self.client.get(reverse("people"))
        self.assertContains(page,"Ana Delgado")
        self.assertContains(page,"Bo Nakamura")

    def test_granting_a_scope_is_an_audited_privileged_action(self):
        self.client.force_login(self.dispatch)
        self.assertEqual(self.client.get(reverse("authority",args=[self.unbound_membership.pk])).status_code,403)
        self.client.force_login(self.owner)
        page=self.client.get(reverse("authority",args=[self.unbound_membership.pk]))
        self.assertContains(page,"Whole company")
        response=self.client.post(reverse("authority",args=[self.unbound_membership.pk]),{"branch":str(self.dallas.pk),"client":"","site":""},follow=True)
        self.assertEqual(response.status_code,200)
        self.assertTrue(AuthorityScope.objects.filter(membership=self.unbound_membership,branch=self.dallas).exists())
        self.assertTrue(AuditEvent.objects.filter(action="authority.scope_granted",target_type="authority_scope").exists())
        self.client.force_login(self.unbound)
        page=self.client.get(reverse("people"))
        self.assertContains(page,"Bo Nakamura")
        self.assertNotContains(page,"Ana Delgado")   # the grant takes effect on the next request
        self.assertContains(page,"Cy Rivera")        # filed to Austin, but stood at a Dallas post

    def test_the_dashboard_counts_only_what_the_actor_can_act_on(self):
        self.client.force_login(self.bounded)
        page=self.client.get(reverse("workspace_people"))
        self.assertContains(page,"Ana Delgado")     # the people workspace is the in-scope roster
        self.assertNotContains(page,"Bo Nakamura")
        bounded=self.client.get(reverse("compliance"),{"kind":"credentials","show":"all"}).context
        self.assertEqual(bounded["totals"]["credentials"],2)   # Ana filed, Cy has nothing on file
        self.assertEqual(bounded["counts"]["credentials"],2)
        self.client.force_login(self.unbound)
        broad=self.client.get(reverse("compliance"),{"kind":"credentials","show":"all"}).context
        self.assertEqual(broad["totals"]["credentials"],3)     # plus Bo

    def test_revoking_a_scope_restores_the_whole_company(self):
        self.client.force_login(self.owner)
        row=AuthorityScope.objects.get(membership=self.austin_membership)
        self.client.post(reverse("authority_revoke",args=[row.pk]))
        self.assertFalse(AuthorityScope.objects.filter(pk=row.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="authority.scope_revoked").exists())
        self.client.force_login(self.bounded)
        page=self.client.get(reverse("people"))
        self.assertContains(page,"Ana Delgado")
        self.assertContains(page,"Bo Nakamura")

    def test_a_grant_must_name_exactly_one_level_and_may_not_be_repeated(self):
        self.client.force_login(self.owner)
        blank=self.client.post(reverse("authority",args=[self.unbound_membership.pk]),{"branch":"","client":"","site":""})
        self.assertEqual(blank.status_code,200)
        self.assertIn("exactly one",str(blank.context["form"].errors))
        both=self.client.post(reverse("authority",args=[self.unbound_membership.pk]),{"branch":str(self.austin.pk),"client":str(self.plaza.pk),"site":""})
        self.assertIn("exactly one",str(both.context["form"].errors))
        self.assertEqual(AuthorityScope.objects.filter(membership=self.unbound_membership).count(),0)
        self.client.post(reverse("authority",args=[self.unbound_membership.pk]),{"branch":str(self.dallas.pk),"client":"","site":""})
        repeat=self.client.post(reverse("authority",args=[self.unbound_membership.pk]),{"branch":str(self.dallas.pk),"client":"","site":""})
        self.assertEqual(AuthorityScope.objects.filter(membership=self.unbound_membership).count(),1)
        self.assertIn("already exists",str(repeat.context["form"].errors).lower())

    def test_a_scope_row_pointing_at_another_company_is_refused(self):
        from django.core.exceptions import ValidationError
        foreign=Organization.objects.create(legal_name="Other LLC",display_name="Other",slug="other-scope")
        foreign_branch=Branch.objects.create(organization=foreign,name="Elsewhere")
        row=AuthorityScope(membership=self.austin_membership,organization=self.org,branch=foreign_branch)
        with self.assertRaises(ValidationError):
            row.full_clean()

    def test_a_renewal_reminder_reaches_the_supervisor_who_covers_the_person(self):
        from .models import Notification
        from .services import queue_compliance_reminders
        queue_compliance_reminders()
        bodies=list(Notification.objects.filter(recipient=self.bounded,event_type="credential.reminder").values_list("body",flat=True))
        self.assertTrue(any("Ana Delgado" in item for item in bodies))
        self.assertFalse(any("Bo Nakamura" in item for item in bodies))
        both=list(Notification.objects.filter(recipient=self.unbound,event_type="credential.reminder").values_list("body",flat=True))
        self.assertTrue(any("Ana Delgado" in item for item in both))
        self.assertTrue(any("Bo Nakamura" in item for item in both))

    def test_a_contract_scope_reaches_the_officers_stood_on_that_contract(self):
        AuthorityScope.objects.create(membership=self.dispatch_membership,organization=self.org,client=self.harbor)
        self.client.force_login(self.dispatch)
        page=self.client.get(reverse("people"))
        self.assertContains(page,"Bo Nakamura")
        self.assertContains(page,"Cy Rivera")     # stood at a Harbor post, though filed to Austin
        self.assertNotContains(page,"Ana Delgado")
        schedule=self.client.get(reverse("schedule"))
        self.assertContains(schedule,"Harbor gate")
        self.assertContains(schedule,"Loan cover")
        self.assertNotContains(schedule,"Plaza lobby")
        # The locations page walks each client's posts, so a contract grant must not render
        # another contract's sites at all.
        locations=self.client.get(reverse("locations"))
        self.assertContains(locations,"Dock")
        self.assertNotContains(locations,"Tower")
        self.assertNotContains(locations,"Meridian Plaza")

    def test_a_site_scope_covers_that_post_and_the_officers_standing_it(self):
        AuthorityScope.objects.filter(membership=self.austin_membership).delete()
        AuthorityScope.objects.create(membership=self.austin_membership,organization=self.org,site=self.tower)
        self.client.force_login(self.bounded)
        page=self.client.get(reverse("people"))
        self.assertContains(page,"Ana Delgado")
        self.assertNotContains(page,"Cy Rivera")   # a post grant is not a branch grant
        schedule=self.client.get(reverse("schedule"))
        self.assertContains(schedule,"Plaza lobby")
        self.assertNotContains(schedule,"Loan cover")
        self.assertNotContains(schedule,"Harbor gate")


class RegisterPaginationTest(TestCase):
    """Every register now says how many rows exist, instead of showing some and implying all.

    Three of these pages used to cap themselves silently (250 punches, 500 audit events, 100
    corrections), and the rest rendered the entire tenant at once. A capped page that reads as a
    complete list is how an operator signs off on a number that was never the whole number.
    """

    def setUp(self):
        from datetime import timedelta
        from uuid import uuid4
        from django.utils import timezone
        User=get_user_model()
        self.owner=User.objects.create_user(username="page-owner@example.com",password="correct horse battery staple")
        self.supervisor=User.objects.create_user(username="page-sup@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Pages LLC",display_name="Pages",slug="pages-llc")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.supervisor,organization=self.org,role=Membership.Role.SUPERVISOR)
        self.branch=Branch.objects.create(organization=self.org,name="Main")
        self.person=Person.objects.create(organization=self.org,branch=self.branch,first_name="Early",last_name="Adams",status=Person.Status.ACTIVE)
        for number in range(1,51):
            Person.objects.create(organization=self.org,branch=self.branch,first_name=f"Guard {number:02d}",last_name="Zulu",status=Person.Status.ACTIVE)
        self.late=Person.objects.create(organization=self.org,branch=self.branch,first_name="Late",last_name="Zulu",status=Person.Status.ACTIVE,email="found-me@example.com")
        now=timezone.now()
        for number in range(60):
            Punch.objects.create(organization=self.org,person=self.person,shift=None,client_event_id=uuid4(),
                kind=Punch.Kind.IN,occurred_at=now-timedelta(minutes=number),review_status=Punch.Review.PENDING)

    def test_the_directory_pages_and_states_the_real_total(self):
        self.client.force_login(self.owner)
        page=self.client.get(reverse("people"))
        self.assertEqual(len(page.context["people"]),50)
        self.assertContains(page,"52 records")
        self.assertContains(page,"page 1 of 2")
        second=self.client.get(reverse("people")+"?page=2")
        self.assertEqual(len(second.context["people"]),2)
        self.assertContains(second,"page 2 of 2")
        self.assertContains(second,"← Previous")

    def test_search_reaches_past_the_rows_on_the_first_page(self):
        self.client.force_login(self.owner)
        found=self.client.get(reverse("people")+"?q=found-me")
        self.assertContains(found,"Late Zulu")           # this row sits on page 2 of the unfiltered list
        self.assertEqual(found.context["paginator"].count,1)
        self.assertNotContains(found,"Guard 01")
        self.assertContains(found,"all of them, on this page")

    def test_a_junk_page_number_shows_the_first_page(self):
        self.client.force_login(self.owner)
        page=self.client.get(reverse("people")+"?page=not-a-number")
        self.assertEqual(page.status_code,200)
        self.assertEqual(page.context["people"].number,1)
        missing=self.client.get(reverse("people")+"?page=99")
        self.assertEqual(missing.context["people"].number,2)   # clamped to the last real page

    def test_the_punch_desk_paginates_where_it_once_stopped_counting(self):
        self.client.force_login(self.supervisor)
        page=self.client.get(reverse("time_review"),{"punches":"all"})
        self.assertEqual(len(page.context["punches"]),50)
        self.assertContains(page,"60 punches · page 1 of 2")


class DocumentRevisionLineageTest(TestCase):
    """A revised handbook is a version of the record it replaces, not a second unrelated file.

    The question a superseded row has to answer is "who signed *this text*". A roster that shows
    everybody outstanding on the new text while the old text still reports complete is the
    difference between a version history and a pile of PDFs — and the old signatures have to
    survive, because they are evidence of what was agreed at the time.
    """

    def setUp(self):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        User=get_user_model()
        self.owner=User.objects.create_user(username="rev-owner@example.com",password="correct horse battery staple")
        self.worker=User.objects.create_user(username="rev-guard@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Revision LLC",display_name="Revision",slug="revision-llc")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.worker,organization=self.org,role=Membership.Role.OFFICER)
        self.person=Person.objects.create(organization=self.org,user=self.worker,first_name="Ana",last_name="Delgado",status=Person.Status.ACTIVE)
        self.handbook=DocumentType.objects.create(organization=self.org,name="Employee handbook",code="handbook",audience=DocumentType.Audience.WORKFORCE,acknowledgment_required=True)
        self.licence=DocumentType.objects.create(organization=self.org,name="Class B licence",code="class-b",audience=DocumentType.Audience.MANAGEMENT)
        self.uploads=SimpleUploadedFile
        media=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        override=override_settings(MEDIA_ROOT=media.name); override.enable()
        self.addCleanup(override.disable); self.addCleanup(media.cleanup)
        self.client.force_login(self.owner)

    def upload(self,document_type,name,body,revises=None):
        payload={"document_type":document_type.pk,"file":self.uploads(name,body,content_type="application/pdf")}
        if revises is not None: payload["revises"]=revises.pk
        return self.client.post(reverse("document_upload"),payload)

    def test_a_revision_supersedes_the_version_it_replaces(self):
        self.upload(self.handbook,"handbook-2024.pdf",b"%PDF-1.4\n2024 text")
        first=PersonDocument.objects.get()
        response=self.upload(self.handbook,"handbook-2026.pdf",b"%PDF-1.4\n2026 text",revises=first)
        self.assertRedirects(response,reverse("documents"))
        second=PersonDocument.objects.get(original_name="handbook-2026.pdf")
        self.assertEqual(second.supersedes_id,first.pk)
        self.assertEqual((first.revision_number,second.revision_number),(1,2))
        self.assertFalse(first.is_current)
        self.assertTrue(second.is_current)
        audit=AuditEvent.objects.get(action="document.uploaded",target_id=str(second.pk))
        self.assertEqual(audit.metadata["revision"],2)
        self.assertEqual(audit.metadata["supersedes"],str(first.pk))

    def test_the_new_text_is_offered_to_the_roster_again_and_the_old_one_is_not(self):
        self.upload(self.handbook,"handbook-2024.pdf",b"%PDF-1.4\n2024 text")
        first=PersonDocument.objects.get()
        self.client.force_login(self.worker)
        self.client.post(reverse("document_acknowledge",args=[first.pk]),{"confirm":"on","signature_name":"Ana Delgado"})
        self.client.force_login(self.owner)
        self.upload(self.handbook,"handbook-2026.pdf",b"%PDF-1.4\n2026 text",revises=first)
        second=PersonDocument.objects.get(original_name="handbook-2026.pdf")
        self.client.force_login(self.worker)
        page=self.client.get(reverse("my_documents"))
        self.assertContains(page,"handbook-2026.pdf")
        self.assertNotContains(page,"handbook-2024.pdf")
        self.assertContains(page,"Review")     # a signature on 2024 is not a signature on 2026
        self.client.force_login(self.owner)
        roster=self.client.get(reverse("document_acknowledgments",args=[second.pk]))
        self.assertContains(roster,"0 acknowledged")
        self.assertContains(roster,"1 outstanding")
        self.assertContains(roster,"Revision 2")

    def test_signing_a_superseded_version_is_refused_and_explained(self):
        from .models import DocumentAcknowledgment
        self.upload(self.handbook,"handbook-2024.pdf",b"%PDF-1.4\n2024 text")
        first=PersonDocument.objects.get()
        self.upload(self.handbook,"handbook-2026.pdf",b"%PDF-1.4\n2026 text",revises=first)
        self.client.force_login(self.worker)
        response=self.client.post(reverse("document_acknowledge",args=[first.pk]),{"confirm":"on","signature_name":"Ana Delgado"},follow=True)
        self.assertRedirects(response,reverse("my_documents"))
        self.assertFalse(DocumentAcknowledgment.objects.filter(document=first).exists())
        self.assertIn("replaced by revision 2",str([str(item) for item in response.context["messages"]]))

    def test_the_superseded_version_stays_readable_and_its_signatures_survive(self):
        from .models import DocumentAcknowledgment
        self.upload(self.handbook,"handbook-2024.pdf",b"%PDF-1.4\n2024 text")
        first=PersonDocument.objects.get()
        self.client.force_login(self.worker)
        self.client.post(reverse("document_acknowledge",args=[first.pk]),{"confirm":"on","signature_name":"Ana Delgado"})
        self.client.force_login(self.owner)
        self.upload(self.handbook,"handbook-2026.pdf",b"%PDF-1.4\n2026 text",revises=first)
        download=self.client.get(reverse("document_download",args=[first.pk]))
        self.assertEqual(download.status_code,200)      # history is not deleted, it is superseded
        self.assertTrue(DocumentAcknowledgment.objects.filter(document=first,person=self.person).exists())
        old=self.client.get(reverse("document_acknowledgments",args=[first.pk]))
        self.assertContains(old,"has been replaced by")
        self.assertContains(old,"Ana Delgado")          # and it says who agreed to that text

    def test_the_queue_counts_the_current_version_once_not_twice(self):
        self.upload(self.handbook,"handbook-2024.pdf",b"%PDF-1.4\n2024 text")
        first=PersonDocument.objects.get()
        self.upload(self.handbook,"handbook-2026.pdf",b"%PDF-1.4\n2026 text",revises=first)
        rows=self.client.get(reverse("compliance"),{"kind":"documents","show":"all"}).context["rows"]
        self.assertEqual([item["reference"] for item in rows],["handbook-2026.pdf"])
        self.assertEqual(rows[0]["note"],"1 of 1 have not acknowledged")

    def test_a_revision_must_replace_a_record_of_the_same_type(self):
        self.upload(self.handbook,"handbook-2024.pdf",b"%PDF-1.4\n2024 text")
        first=PersonDocument.objects.get()
        response=self.upload(self.licence,"licence.pdf",b"%PDF-1.4\nlicence",revises=first)
        self.assertEqual(response.status_code,200)
        self.assertIn("record of the same type",str(response.context["form"].errors))
        self.assertFalse(PersonDocument.objects.filter(original_name="licence.pdf").exists())

    def test_a_record_cannot_supersede_itself(self):
        from django.core.exceptions import ValidationError
        self.upload(self.handbook,"handbook-2024.pdf",b"%PDF-1.4\n2024 text")
        first=PersonDocument.objects.get()
        first.supersedes_id=first.pk
        with self.assertRaises(ValidationError):
            first.full_clean()


class AvailabilityAndTimeOffTest(TestCase):
    """What the officer says about their own hours, and what it does to a post.

    Two different weights, deliberately: an approved absence refuses an assignment, because
    putting a guard on a post they were granted off is a broken promise twice over; a post
    outside the stated availability only warns, because a coverage gap is not solved by hiding
    the only officer who could fill it. And no rule here touches the clock — a punch during
    approved leave is time that was worked, and the evidence is not the software's to discard.
    """

    def setUp(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        from django.utils import timezone
        User=get_user_model()
        self.owner=User.objects.create_user(username="pto-owner@example.com",password="correct horse battery staple")
        self.officer=User.objects.create_user(username="pto-guard@example.com",email="pto-guard@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Rota LLC",display_name="Rota",slug="rota-llc")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.officer,organization=self.org,role=Membership.Role.OFFICER)
        self.branch=Branch.objects.create(organization=self.org,name="North")
        self.account=Client.objects.create(organization=self.org,name="Meridian")
        self.site=Site.objects.create(organization=self.org,client=self.account,branch=self.branch,name="Desk",address="1 Meridian")
        self.ana=Person.objects.create(organization=self.org,user=self.officer,branch=self.branch,first_name="Ana",last_name="Delgado",status=Person.Status.ACTIVE)
        self.bo=Person.objects.create(organization=self.org,branch=self.branch,first_name="Bo",last_name="Nakamura",status=Person.Status.ACTIVE)
        self.zone=ZoneInfo("America/Chicago")
        self.timedelta=timedelta
        # A fixed, past week keeps the arithmetic readable: Mon 2 Feb 2026 through Fri 6 Feb.
        self.day=lambda number,hour=8: self.datetime(2026,2,number,hour,tzinfo=self.zone)
        self.datetime=datetime
        self.timezone=timezone

    def post(self,officer,starts,hours=10,status=None,post_name="Cover"):
        return Shift.objects.create(organization=self.org,site=self.site,officer=officer,starts_at=starts,
            ends_at=starts+self.timedelta(hours=hours),status=status or Shift.Status.PUBLISHED,post_name=post_name)

    def messages(self,response):
        return [str(item) for item in response.context["messages"]]

    def test_an_officer_states_a_weekly_pattern_a_manager_can_see(self):
        self.client.force_login(self.officer)
        response=self.client.post(reverse("availability"),{"weekday":0,"starts_at":"08:00","ends_at":"18:00"},follow=True)
        self.assertEqual(response.status_code,200)
        rule=AvailabilityRule.objects.get()
        self.assertEqual((rule.person_id,rule.weekday),(self.ana.pk,0))
        self.assertIn("Monday 08:00–18:00",self.messages(response)[0])
        self.client.force_login(self.owner)
        profile=self.client.get(reverse("person_detail",args=[self.ana.pk]),{"tab":"time"})
        self.assertContains(profile,"08:00")
        self.assertContains(profile,"Monday")

    def test_an_overnight_window_owns_the_morning_it_ends_in(self):
        # The way a guard describes a Friday night: one window, 18:00 to 06:00.
        rule=AvailabilityRule.objects.create(organization=self.org,person=self.ana,weekday=4,starts_at=self.datetime.time(self.datetime(2026,2,6,18)),ends_at=self.datetime.time(self.datetime(2026,2,6,6)))
        self.assertTrue(rule.covers(self.day(6,22)))          # Friday night
        self.assertTrue(rule.covers(self.day(7,2)))           # Saturday morning, from Friday's window
        self.assertFalse(rule.covers(self.day(7,7)))          # the window closed at 06:00
        self.assertFalse(rule.covers(self.day(6,12)))         # Friday midday is not in it
        self.assertFalse(rule.covers(self.day(15,22)))        # Sunday night is a different window

    def test_approved_leave_refuses_the_assignment(self):
        leave=TimeOffRequest.objects.create(organization=self.org,person=self.ana,status=TimeOffRequest.Status.APPROVED,
            starts_at=self.day(5),ends_at=self.day(6,18))
        self.client.force_login(self.owner)
        response=self.client.post(reverse("shift_create"),{"site":self.site.pk,"officer":self.ana.pk,
            "starts_at":"2026-02-05T08:00","ends_at":"2026-02-05T18:00","status":Shift.Status.PUBLISHED,"post_name":"Friday desk","required_credentials":[]},follow=True)
        self.assertFalse(Shift.objects.filter(post_name="Friday desk").exists())
        self.assertIn("On approved leave",str(response.context["form"].errors))

    def test_approved_leave_never_refuses_the_clock(self):
        from uuid import uuid4
        from .services import record_punch
        # The clock only accepts recent evidence, so this one runs on "now" rather than the
        # fixture week: what is being proved is that leave does not stop a punch.
        starts=self.timezone.now().replace(minute=0,second=0,microsecond=0)
        shift=self.post(self.ana,starts,hours=8)
        TimeOffRequest.objects.create(organization=self.org,person=self.ana,status=TimeOffRequest.Status.APPROVED,
            starts_at=starts-self.timedelta(hours=1),ends_at=starts+self.timedelta(hours=12))
        punch,created=record_punch(organization=self.org,person=self.ana,shift=shift,client_event_id=uuid4(),
            kind=Punch.Kind.IN,occurred_at=starts)
        # Time worked on a day that was granted off is still time worked: the punch is kept and
        # accepted, because the evidence is not the software's to discard.
        self.assertTrue(created)
        self.assertEqual(punch.review_status,Punch.Review.ACCEPTED)
        self.assertEqual(punch.exception_reason,"")

    def test_a_post_outside_the_stated_availability_is_warned_and_still_saved(self):
        AvailabilityRule.objects.create(organization=self.org,person=self.ana,weekday=0,starts_at=self.datetime.time(self.datetime(2026,2,2,8)),ends_at=self.datetime.time(self.datetime(2026,2,2,16)))
        self.client.force_login(self.owner)
        response=self.client.post(reverse("shift_create"),{"site":self.site.pk,"officer":self.ana.pk,
            "starts_at":"2026-02-02T19:00","ends_at":"2026-02-02T22:00","status":Shift.Status.PUBLISHED,"post_name":"Evening","required_credentials":[]},follow=True)
        self.assertRedirects(response,reverse("schedule"))
        self.assertTrue(Shift.objects.filter(post_name="Evening").exists())
        warnings=[item for item in self.messages(response) if "availability" in item.lower()]
        self.assertTrue(warnings)
        self.assertIn("no availability window covering Mon 19:00",warnings[0])

    def test_someone_who_has_stated_nothing_is_never_warned_about_availability(self):
        self.client.force_login(self.owner)
        response=self.client.post(reverse("shift_create"),{"site":self.site.pk,"officer":self.ana.pk,
            "starts_at":"2026-02-02T18:00","ends_at":"2026-02-02T22:00","status":Shift.Status.PUBLISHED,"post_name":"Evening","required_credentials":[]},follow=True)
        self.assertFalse([item for item in self.messages(response) if "availability" in item.lower()])

    def test_the_overtime_threshold_is_shown_before_the_post_is_stood(self):
        TimePolicy.objects.create(organization=self.org,timezone="America/Chicago",workweek_start=0,overtime_after_hours=40)
        for number in (2,3,4,5):
            self.post(self.ana,self.day(number,8),hours=10,post_name=f"Night {number}")
        self.client.force_login(self.owner)
        response=self.client.post(reverse("shift_create"),{"site":self.site.pk,"officer":self.ana.pk,
            "starts_at":"2026-02-06T08:00","ends_at":"2026-02-06T18:00","status":Shift.Status.PUBLISHED,"post_name":"Sixth","required_credentials":[]},follow=True)
        self.assertTrue(Shift.objects.filter(post_name="Sixth").exists())
        premium=[item for item in self.messages(response) if "premium time" in item]
        self.assertTrue(premium)
        self.assertIn("about 50.0 hours",premium[0])

    def test_an_officer_asks_and_the_managers_who_cover_them_are_told(self):
        from .models import Notification
        self.client.force_login(self.officer)
        response=self.client.post(reverse("my_time_off"),{"starts_at":"2026-02-09T00:00","ends_at":"2026-02-13T00:00","reason":"Family"},follow=True)
        self.assertRedirects(response,reverse("my_time_off"))
        item=TimeOffRequest.objects.get()
        self.assertEqual((item.status,item.person_id,item.requested_by_id),(TimeOffRequest.Status.REQUESTED,self.ana.pk,self.officer.pk))
        recipients=set(Notification.objects.filter(event_type="timeoff.requested").values_list("recipient_id",flat=True))
        self.assertEqual(recipients,{self.owner.pk,self.officer.pk})   # the requester gets an in-app receipt
        self.assertTrue(AuditEvent.objects.filter(action="timeoff.requested").exists())

    def test_a_manager_approves_and_the_officer_is_told_the_decision(self):
        from .models import Notification
        item=TimeOffRequest.objects.create(organization=self.org,person=self.ana,status=TimeOffRequest.Status.REQUESTED,
            starts_at=self.day(9),ends_at=self.day(13))
        self.client.force_login(self.owner)
        response=self.client.post(reverse("time_off_decide",args=[item.pk]),{"action":"approved","note":"Book the cover first"},follow=True)
        self.assertRedirects(response,reverse("time_off"))
        item.refresh_from_db()
        self.assertEqual((item.status,item.decided_by_id),(TimeOffRequest.Status.APPROVED,self.owner.pk))
        self.assertTrue(Notification.objects.filter(recipient=self.officer,event_type="timeoff.approved").exists())
        self.client.force_login(self.officer)
        self.assertContains(self.client.get(reverse("my_time_off")),"Approved")

    def test_approving_leave_names_the_live_posts_it_falls_inside_and_erases_none(self):
        standing=self.post(self.ana,self.day(9,8),hours=10,post_name="Monday desk")
        item=TimeOffRequest.objects.create(organization=self.org,person=self.ana,status=TimeOffRequest.Status.REQUESTED,
            starts_at=self.day(9),ends_at=self.day(10))
        self.client.force_login(self.owner)
        queue=self.client.get(reverse("time_off"))
        self.assertContains(queue,"Monday desk")      # the hole is shown before the decision
        response=self.client.post(reverse("time_off_decide",args=[item.pk]),{"action":"approved","note":""},follow=True)
        standing.refresh_from_db()
        self.assertEqual(standing.status,Shift.Status.PUBLISHED)   # approval does not cancel coverage
        self.assertEqual(standing.officer_id,self.ana.pk)
        self.assertTrue(any("1 live post" in text for text in self.messages(response)))

    def test_a_request_can_be_withdrawn_by_the_officer_only(self):
        item=TimeOffRequest.objects.create(organization=self.org,person=self.bo,status=TimeOffRequest.Status.REQUESTED,
            starts_at=self.day(9),ends_at=self.day(10))
        self.client.force_login(self.officer)
        self.assertEqual(self.client.post(reverse("my_time_off_cancel",args=[item.pk])).status_code,404)
        own=TimeOffRequest.objects.create(organization=self.org,person=self.ana,status=TimeOffRequest.Status.REQUESTED,
            starts_at=self.day(9),ends_at=self.day(10))
        self.client.post(reverse("my_time_off_cancel",args=[own.pk]))
        own.refresh_from_db()
        self.assertEqual(own.status,TimeOffRequest.Status.CANCELLED)

    def test_the_leave_queue_is_bounded_by_the_actors_authority(self):
        User=get_user_model()
        dallas=Branch.objects.create(organization=self.org,name="South")
        carol=Person.objects.create(organization=self.org,branch=dallas,first_name="Cy",last_name="Rivera",status=Person.Status.ACTIVE)
        supervisor=User.objects.create_user(username="pto-sup@example.com",password="correct horse battery staple")
        membership=Membership.objects.create(user=supervisor,organization=self.org,role=Membership.Role.SUPERVISOR)
        AuthorityScope.objects.create(membership=membership,organization=self.org,branch=self.branch)
        for person in (self.ana,carol):
            TimeOffRequest.objects.create(organization=self.org,person=person,status=TimeOffRequest.Status.REQUESTED,
                starts_at=self.day(9),ends_at=self.day(10))
        self.client.force_login(supervisor)
        page=self.client.get(reverse("time_off"))
        self.assertContains(page,"Ana Delgado")
        self.assertNotContains(page,"Cy Rivera")
        carol_row=TimeOffRequest.objects.get(person=carol)
        self.assertEqual(self.client.post(reverse("time_off_decide",args=[carol_row.pk]),{"action":"approved"}).status_code,404)

    def test_a_leave_request_range_must_be_ordered(self):
        self.client.force_login(self.officer)
        response=self.client.post(reverse("my_time_off"),{"starts_at":"2026-02-13T00:00","ends_at":"2026-02-09T00:00","reason":""})
        self.assertIn("Leave must end after it starts",str(response.context["form"].errors))
        self.assertEqual(TimeOffRequest.objects.count(),0)


class OperationalReportsTest(TestCase):
    """The dashboard's three numbers, computed by the code the working screens already use.

    The dangerous way to build a report is a second implementation of "what counts". These tests
    pin the shared-path guarantee and the denominator behaviour: an empty schedule must report
    "nothing to measure", never 100%, and a post whose officer lapsed today is not coverage.
    """

    def setUp(self):
        from datetime import timedelta
        from uuid import uuid4
        from django.utils import timezone
        self.timedelta=timedelta
        User=get_user_model()
        self.owner=User.objects.create_user(username="report-owner@example.com",password="correct horse battery staple")
        self.bounded=User.objects.create_user(username="report-austin@example.com",password="correct horse battery staple")
        self.officer_user=User.objects.create_user(username="report-guard@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Reports LLC",display_name="Reports",slug="reports-llc")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        self.membership=Membership.objects.create(user=self.bounded,organization=self.org,role=Membership.Role.SUPERVISOR)
        self.austin=Branch.objects.create(organization=self.org,name="Austin")
        self.dallas=Branch.objects.create(organization=self.org,name="Dallas")
        account=Client.objects.create(organization=self.org,name="Meridian")
        self.tower=Site.objects.create(organization=self.org,client=account,branch=self.austin,name="Tower",address="1 Plaza")
        self.dock=Site.objects.create(organization=self.org,client=account,branch=self.dallas,name="Dock",address="2 Dock")
        self.registration=CredentialType.objects.create(organization=self.org,name="Guard registration",code="reg",
            applies_to=["unarmed"],warning_days=30,blocks_scheduling=True)
        self.ready=Person.objects.create(organization=self.org,user=self.officer_user,branch=self.austin,first_name="Quinn",last_name="Ready",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        self.lapsed=Person.objects.create(organization=self.org,branch=self.austin,first_name="Lou",last_name="Lapsed",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        self.distant=Person.objects.create(organization=self.org,branch=self.dallas,first_name="Dee",last_name="Dallas",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        later=timezone.localdate()+timedelta(days=200)
        for person in (self.ready,self.distant):
            Credential.objects.create(organization=self.org,person=person,credential_type=self.registration,status=Credential.Status.ACTIVE,expires_on=later)
        Credential.objects.create(organization=self.org,person=self.lapsed,credential_type=self.registration,status=Credential.Status.ACTIVE,expires_on=timezone.localdate()-timedelta(days=1))
        now=timezone.now()
        self.covered=self.post(self.tower,self.ready,now+timedelta(days=1))
        # The risk post has to *declare* the requirement: per the product ruling recorded in
        # docs/feature-status.md, scheduling is gated by what the post requires, not by any
        # lapse on the officer's record, so an unrequired expired licence is not a stop here.
        self.risk=self.post(self.tower,self.lapsed,now+timedelta(days=2),post_name="Armed lobby")
        self.risk.required_credentials.add(self.registration)
        self.open_post=self.post(self.tower,None,now+timedelta(days=3),post_name="Uncovered",status=Shift.Status.PUBLISHED)
        self.plan=self.post(self.tower,self.ready,now+timedelta(days=4),status=Shift.Status.DRAFT)
        self.other_branch=self.post(self.dock,self.distant,now+timedelta(days=1))
        past=self.post(self.tower,self.ready,now-timedelta(days=2),hours=8,status=Shift.Status.COMPLETED,post_name="Closed tour")
        Punch.objects.create(organization=self.org,person=self.ready,shift=past,client_event_id=uuid4(),kind=Punch.Kind.IN,occurred_at=past.starts_at)
        Punch.objects.create(organization=self.org,person=self.ready,shift=past,client_event_id=uuid4(),kind=Punch.Kind.OUT,occurred_at=past.ends_at)
        unclosed=self.post(self.tower,self.ready,now-timedelta(days=1),hours=8,status=Shift.Status.COMPLETED,post_name="Forgot the out")
        Punch.objects.create(organization=self.org,person=self.ready,shift=unclosed,client_event_id=uuid4(),kind=Punch.Kind.IN,occurred_at=unclosed.starts_at)
        self.timedelta=timedelta

    def post(self,site,officer,starts,hours=10,status=Shift.Status.PUBLISHED,post_name="Post"):
        return Shift.objects.create(organization=self.org,site=site,officer=officer,starts_at=starts,
            ends_at=starts+self.timedelta(hours=hours),status=status,post_name=post_name)

    def test_the_report_and_the_queue_cannot_disagree(self):
        # Both pages read compliance_attendance; this asserts the numbers the two screens show
        # are the same numbers, which is the only guarantee that makes a rate trustworthy.
        self.client.force_login(self.owner)
        queue=self.client.get(reverse("compliance"),{"show":"all"}).context
        report=self.client.get(reverse("reports")).context["summary"]
        self.assertEqual(report["total"], sum(queue["totals"].values()))
        self.assertEqual(report["attention"], sum(queue["counts"].values()))

    def test_a_bounded_supervisor_sees_his_branch_not_the_company(self):
        AuthorityScope.objects.create(membership=self.membership,organization=self.org,branch=self.austin)
        self.client.force_login(self.owner)
        company=self.client.get(reverse("reports")).context["summary"]
        self.client.force_login(self.bounded)
        page=self.client.get(reverse("reports"))
        branch=page.context["summary"]
        self.assertLess(branch["total"], company["total"])
        self.assertNotIn(self.distant.pk, [row["person"].pk for row in page.context["attendance"]["credentials"]["rows"]])
        # Coverage is bounded the same way: Dee Dallas's post is not this supervisor's gap to see.
        self.assertEqual([shift.pk for shift in page.context["coverage"]["filled"]],[self.covered.pk])
        self.assertContains(page,"Austin branch")

    def test_an_elapsed_officer_is_not_coverage(self):
        self.client.force_login(self.owner)
        coverage=self.client.get(reverse("reports")).context["coverage"]
        self.assertEqual([item["shift"].pk for item in coverage["at_risk"]],[self.risk.pk])
        self.assertEqual([shift.pk for shift in coverage["unfilled"]],[self.open_post.pk])
        # Deterministic order, not whichever page MySQL happened to read first: the two posts
        # start at the same instant, so the report breaks the tie by site name and pk.
        self.assertEqual([shift.pk for shift in coverage["filled"]],[self.other_branch.pk,self.covered.pk])
        self.assertEqual(coverage["drafts_excluded"],1)      # the plan is reported, not counted
        self.assertEqual(coverage["total"],4)
        self.assertEqual(coverage["rate"],50.0)

    def test_an_empty_window_reports_nothing_instead_of_perfection(self):
        # An empty schedule is where a naive report writes 100%. Both rates must say
        # "nothing to measure" instead, through None rather than an invented zero.
        from django.utils import timezone
        from .scope import for_membership
        from .services import coverage_report, tour_completion
        far = timezone.localtime(timezone.now()).replace(hour=0, minute=0, second=0, microsecond=0) + self.timedelta(days=400)
        scope = for_membership(None)
        quiet = coverage_report(self.org, scope, far, far + self.timedelta(days=7))
        self.assertEqual(quiet["total"], 0)
        self.assertIsNone(quiet["rate"])
        empty = tour_completion(self.org, scope, far, far + self.timedelta(days=7))
        self.assertEqual(empty["total"], 0)
        self.assertIsNone(empty["rate"])
        # A window with posts but no published ones is still nothing, not perfect coverage.
        only_drafts = coverage_report(self.org, scope, self.plan.starts_at, self.plan.ends_at)
        self.assertEqual(only_drafts["total"], 0)
        self.assertEqual(only_drafts["drafts_excluded"], 1)

    def test_unclosed_tours_are_listed_with_what_is_missing(self):
        self.client.force_login(self.owner)
        closed=self.client.get(reverse("reports")).context["closed"]
        self.assertEqual(closed["total"],2)
        self.assertEqual(closed["closed"],1)
        self.assertEqual([item["shift"].post_name for item in closed["open"]],["Forgot the out"])
        self.assertEqual(closed["open"][0]["missing"],"clock-out")
        self.assertEqual(closed["rate"],50.0)

    def test_junk_and_absurd_windows_are_clamped_not_rejected(self):
        self.client.force_login(self.owner)
        for value,expected in (("nonsense",7),("0",1),("9999",56),("14",14)):
            context=self.client.get(reverse("reports"),{"days":value}).context
            self.assertEqual(context["days"],expected)

    def test_an_officer_sees_no_reports(self):
        self.client.force_login(self.officer_user)
        self.assertEqual(self.client.get(reverse("reports")).status_code,403)


class EvidencePolicyInheritanceTest(TestCase):
    """A contract or a property may run its own clock rule, and every number says which one did.

    The company rounding rule cannot be the last word on a post whose agreement says otherwise,
    and a payroll figure that will not name the rule that produced it is not reproducible. But a
    pay period cannot begin on two days at once either, so what is overridable here is evidence
    and rounding — never the calendar.
    """

    def setUp(self):
        from datetime import datetime, timedelta
        from decimal import Decimal
        from uuid import uuid4
        from zoneinfo import ZoneInfo
        User=get_user_model()
        self.owner=User.objects.create_user(username="policy-owner@example.com",password="correct horse battery staple")
        self.supervisor=User.objects.create_user(username="policy-sup@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Policy LLC",display_name="Policy",slug="policy-llc")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.supervisor,organization=self.org,role=Membership.Role.SUPERVISOR)
        self.company=TimePolicy.objects.create(organization=self.org,timezone="America/Chicago",workweek_start=0,
            overtime_after_hours=Decimal("40.00"),rounding_mode=TimePolicy.RoundingMode.EXACT,rounding_minutes=1,require_geofence=True)
        self.hospital=Client.objects.create(organization=self.org,name="St Luke's Hospital")
        self.warehouse=Client.objects.create(organization=self.org,name="Dockside Storage")
        self.ward=Site.objects.create(organization=self.org,client=self.hospital,name="Ward 4",address="1 Medical",
            latitude=Decimal("32.776700"),longitude=Decimal("-96.797000"),geofence_radius_meters=150)
        self.reception=Site.objects.create(organization=self.org,client=self.hospital,name="Night reception",address="1 Medical gate",
            latitude=Decimal("32.776700"),longitude=Decimal("-96.797000"),geofence_radius_meters=150)
        self.dock=Site.objects.create(organization=self.org,client=self.warehouse,name="Gate",address="2 Dock",
            latitude=Decimal("32.750000"),longitude=Decimal("-96.810000"),geofence_radius_meters=200)
        TimePolicyOverride.objects.create(organization=self.org,client=self.hospital,
            rounding_mode=TimePolicy.RoundingMode.NEAREST,rounding_minutes=15,authorized_by=self.owner)
        TimePolicyOverride.objects.create(organization=self.org,site=self.reception,require_geofence=False,authorized_by=self.owner)
        self.guard=User.objects.create_user(username="policy-guard@example.com",password="correct horse battery staple")
        self.person=Person.objects.create(organization=self.org,user=self.guard,first_name="Ana",last_name="Delgado",status=Person.Status.ACTIVE)
        self.zone=ZoneInfo("America/Chicago"); self.timedelta=timedelta; self.datetime=datetime; self.uuid4=uuid4
        self.starts=datetime(2026,2,2,8,0,tzinfo=self.zone)

    def tour(self,site,worked_minutes,offset_days=0):
        from .models import Punch
        starts=self.starts+self.timedelta(days=offset_days)
        shift=Shift.objects.create(organization=self.org,site=site,officer=self.person,starts_at=starts,
            ends_at=starts+self.timedelta(minutes=worked_minutes),status=Shift.Status.PUBLISHED,post_name="Tour")
        Punch.objects.create(organization=self.org,person=self.person,shift=shift,client_event_id=self.uuid4(),kind=Punch.Kind.IN,occurred_at=starts)
        Punch.objects.create(organization=self.org,person=self.person,shift=shift,client_event_id=self.uuid4(),kind=Punch.Kind.OUT,occurred_at=starts+self.timedelta(minutes=worked_minutes))
        return shift

    def test_a_site_rule_beats_the_contract_which_beats_the_company(self):
        from .services import effective_clock_policy
        dock=effective_clock_policy(self.org,self.dock)
        self.assertEqual(dock.rounding["source"],"company")
        self.assertEqual(dock.rounding["mode"],TimePolicy.RoundingMode.EXACT)
        self.assertEqual(dock.geofence["source"],"company")
        ward=effective_clock_policy(self.org,self.ward)
        self.assertEqual(ward.rounding["source"],"contract")
        self.assertEqual(ward.rounding["minutes"],15)
        reception=effective_clock_policy(self.org,self.reception)
        # The two values a single post consumes come from two different levels, and each says so.
        self.assertEqual(reception.geofence["source"],"site")
        self.assertFalse(reception.geofence["required"])
        self.assertEqual(reception.rounding["source"],"contract")
        self.assertEqual(reception.rounding["minutes"],15)
        # Each value is stamped with the row that actually chose it, on the same post.
        contract_row=TimePolicyOverride.objects.get(client=self.hospital)
        site_row=TimePolicyOverride.objects.get(site=self.reception)
        self.assertEqual(reception.rounding["version"],f"{contract_row.pk}:1")
        self.assertEqual(reception.geofence["version"],f"{site_row.pk}:1")

    def test_an_override_states_a_delta_and_never_the_calendar(self):
        from .services import effective_clock_policy
        reception=effective_clock_policy(self.org,self.reception)
        self.assertEqual(reception.require_geofence,False)
        self.assertEqual(reception.overtime_after_hours,self.company.overtime_after_hours)
        self.assertEqual(reception.workweek_start,0)
        self.assertEqual(reception.timezone,"America/Chicago")
        self.assertTrue(reception.company.require_geofence)   # the baseline row is untouched

    def test_the_export_carries_the_worked_time_and_the_rule_that_rounded_it(self):
        from .services import PAYROLL_EXPORT_FIELDS, payroll_rows
        self.tour(self.ward,487)               # 8h07m under a contract that rounds to nearest 15
        self.tour(self.dock,487,offset_days=1) # company baseline: exact
        rows={row["site"]:row for row in payroll_rows(self.org,self.datetime(2026,2,1,tzinfo=self.zone),self.datetime(2026,2,8,tzinfo=self.zone))}
        self.assertEqual(float(rows["Ward 4"]["total_hours"]),8.00)
        self.assertEqual(float(rows["Ward 4"]["raw_hours"]),8.12)
        self.assertEqual(rows["Ward 4"]["policy_source"],"contract")
        self.assertEqual(rows["Ward 4"]["rounding_mode"],"nearest")
        self.assertEqual(float(rows["Gate"]["total_hours"]),8.12)
        self.assertEqual(rows["Gate"]["policy_source"],"company")
        self.assertEqual(float(rows["Gate"]["raw_hours"]),float(rows["Gate"]["total_hours"]))
        for field in ("raw_hours","rounding_mode","rounding_minutes","policy_source","policy_version"):
            self.assertIn(field,PAYROLL_EXPORT_FIELDS)

    def test_the_fence_judged_against_a_punch_is_the_one_that_post_resolves_to(self):
        from datetime import timedelta
        from uuid import uuid4
        from django.utils import timezone
        from .models import AuditEvent, Punch
        from .services import record_punch
        # record_punch refuses evidence older than twelve hours, so this one runs on "now"
        # rather than the fixture's fixed February week.
        starts=timezone.now().replace(minute=0,second=0,microsecond=0)-timedelta(hours=2)
        shift=Shift.objects.create(organization=self.org,site=self.reception,officer=self.person,starts_at=starts,
            ends_at=starts+timedelta(hours=8),status=Shift.Status.PUBLISHED,post_name="Desk")
        punch,_=record_punch(organization=self.org,person=self.person,shift=shift,client_event_id=uuid4(),
            kind=Punch.Kind.IN,occurred_at=starts)          # no coordinates at all
        self.assertEqual(punch.review_status,Punch.Review.ACCEPTED)   # the property waived the fence
        metadata=AuditEvent.objects.get(action="punch.recorded",target_id=str(punch.pk)).metadata
        self.assertEqual(metadata["policy_source"],"site")
        self.assertFalse(metadata["require_geofence"])
        # Eleven hours earlier: still inside the twelve-hour window, and it does not overlap the
        # desk tour, which the clock gate would refuse for its own reason.
        later=starts-timedelta(hours=9)
        other=Shift.objects.create(organization=self.org,site=self.ward,officer=self.person,
            starts_at=later,ends_at=later+timedelta(hours=8),status=Shift.Status.PUBLISHED,post_name="Ward")
        flagged,_=record_punch(organization=self.org,person=self.person,shift=other,client_event_id=uuid4(),
            kind=Punch.Kind.IN,occurred_at=later)
        self.assertEqual(flagged.review_status,Punch.Review.PENDING)   # same company, no waiver here
        self.assertIn("Location was not supplied",flagged.exception_reason)

    def test_the_hub_shows_what_a_baseline_change_would_and_would_not_reach(self):
        self.client.force_login(self.owner)
        page=self.client.get(reverse("time_policy"))
        self.assertContains(page,"Version 1")
        self.assertContains(page,"Night reception")
        self.assertContains(page,"(every post)")     # the contract's own row
        self.assertContains(page,"keep their own rounding")
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse("time_policy")).status_code,403)

    def test_a_rule_must_name_one_level_and_both_halves_of_a_rounding_rule(self):
        self.client.force_login(self.owner)
        none=self.client.post(reverse("time_policy_override_create"),
            {"client":"","site":"","require_geofence":"","rounding_mode":"","rounding_minutes":""})
        self.assertIn("Choose the level",str(none.context["form"].errors))
        both=self.client.post(reverse("time_policy_override_create"),
            {"client":self.warehouse.pk,"site":self.dock.pk,"require_geofence":"yes","rounding_mode":"","rounding_minutes":""})
        self.assertIn("Choose the level",str(both.context["form"].errors))
        half=self.client.post(reverse("time_policy_override_create"),
            {"client":self.warehouse.pk,"site":"","require_geofence":"","rounding_mode":"up","rounding_minutes":""})
        self.assertIn("Choose an interval",str(half.context["form"].errors))
        self.assertEqual(TimePolicyOverride.objects.filter(client=self.warehouse).count(),0)

    def test_a_second_rule_for_the_same_target_is_refused_before_it_reaches_the_database(self):
        # MySQL cannot enforce a conditional unique constraint, so this check is the only thing
        # standing between two rules for one site and an unresolvable "which one applies".
        yard=Site.objects.create(organization=self.org,client=self.warehouse,name="Yard",address="3 Dock")
        TimePolicyOverride.objects.create(organization=self.org,site=yard,require_geofence=True,authorized_by=self.owner)
        self.client.force_login(self.owner)
        response=self.client.post(reverse("time_policy_override_create"),
            {"client":"","site":yard.pk,"require_geofence":"no","rounding_mode":"","rounding_minutes":""})
        self.assertIn("already has its own rule",str(response.context["form"].errors))
        self.assertEqual(TimePolicyOverride.objects.filter(site=yard).count(),1)

    def test_editing_one_value_leaves_the_untouched_waiver_in_place(self):
        # The tri-state trap: a select that renders "inherit" for an explicit False would, on
        # the next save, quietly put the fence back up at a gate the client waived.
        row=TimePolicyOverride.objects.get(site=self.reception)
        self.client.force_login(self.owner)
        page=self.client.get(reverse("time_policy_override",args=[row.pk]))
        # The guarantee is that the screen shows the waiver it is editing, rather than an empty
        # select that would silently put the fence back up on the next save.
        self.assertContains(page,'value="no" selected',html=False)
        self.client.post(reverse("time_policy_override",args=[row.pk]),{"client":"","site":self.reception.pk,
            "require_geofence":"no","rounding_mode":"down","rounding_minutes":"6"})
        row.refresh_from_db()
        self.assertFalse(row.require_geofence)
        self.assertEqual(row.rounding_minutes,6)
        self.assertEqual(row.revision,2)                    # a real change advanced the version
        self.client.post(reverse("time_policy_override",args=[row.pk]),{"client":"","site":self.reception.pk,
            "require_geofence":"no","rounding_mode":"down","rounding_minutes":"6"})
        row.refresh_from_db()
        self.assertEqual(row.revision,2)                    # saving identical values did not

    def test_a_rule_can_be_removed_and_the_higher_scope_governs_again(self):
        from .services import effective_clock_policy
        row=TimePolicyOverride.objects.get(site=self.reception)
        self.client.force_login(self.owner)
        response=self.client.post(reverse("time_policy_override_remove",args=[row.pk]),follow=True)
        self.assertEqual(response.status_code,200)
        self.assertFalse(TimePolicyOverride.objects.filter(pk=row.pk).exists())
        self.assertTrue(effective_clock_policy(self.org,self.reception).require_geofence)
        self.assertEqual(effective_clock_policy(self.org,self.reception).geofence["source"],"company")
        # Removing the site waiver leaves the contract rounding where it was.
        self.assertEqual(effective_clock_policy(self.org,self.reception).rounding["source"],"contract")
        self.assertTrue(AuditEvent.objects.filter(action="time_policy.override_removed").exists())

    def test_a_rule_may_not_point_at_another_companys_contract(self):
        from django.core.exceptions import ValidationError
        foreign=Organization.objects.create(legal_name="Other LLC",display_name="Other",slug="other-policy")
        foreign_client=Client.objects.create(organization=foreign,name="Elsewhere Care")
        with self.assertRaises(ValidationError):
            TimePolicyOverride(organization=self.org,client=foreign_client,require_geofence=True).full_clean()

    def test_the_schedule_names_the_rule_that_will_be_applied_to_the_post(self):
        from datetime import datetime, time as clock_time, timedelta
        from django.utils import timezone
        from .services import schedule_week_start
        # Anchored on the schedule page's own Monday-to-Sunday window rather than on `now + 1 day`.
        # A Sunday run put tomorrow in the *following* week, and a week-scoped page is right to hide a
        # post that is not in the week it is showing — the fixture was asserting about the clock.
        starts=timezone.make_aware(datetime.combine(schedule_week_start()+timedelta(days=1), clock_time(8,0)))
        Shift.objects.create(organization=self.org,site=self.reception,officer=self.person,starts_at=starts,
            ends_at=starts+timedelta(hours=8),status=Shift.Status.PUBLISHED,post_name="Waived gate")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("schedule")+"?layout=list"),"nearest 15 min · contract")

    def test_a_rule_stays_editable_after_its_site_is_deactivated(self):
        # The target list offers active sites only, which would otherwise strand the waiver:
        # taking a fence back down at a closed property has to remain possible.
        row=TimePolicyOverride.objects.get(site=self.reception)
        self.reception.active=False
        self.reception.save(update_fields=["active"])
        self.client.force_login(self.owner)
        response=self.client.post(reverse("time_policy_override",args=[row.pk]),{"client":"","site":self.reception.pk,
            "require_geofence":"","rounding_mode":"","rounding_minutes":""})
        self.assertRedirects(response,reverse("time_policy"))
        row.refresh_from_db()
        self.assertIsNone(row.require_geofence)          # the waiver is withdrawn
        self.assertEqual(row.revision,2)                 # and that is a real change, so it counts
        from .services import effective_clock_policy
        self.assertTrue(effective_clock_policy(self.org,self.reception).require_geofence)

    def test_the_locations_page_marks_which_posts_carry_their_own_rule(self):
        self.client.force_login(self.owner)
        page=self.client.get(reverse("locations"))
        self.assertContains(page,"Own clock rule")           # Night reception, waived fence
        self.assertEqual(page.content.decode().count("Own clock rule"),1)

    def test_saving_a_baseline_that_did_not_change_does_not_advance_its_version(self):
        self.client.force_login(self.owner)
        # Every watched field has to be in the payload, including the two evidence switches: a
        # checkbox the form does not receive arrives as False, which is a real edit to the rule and so
        # a real new version. Omitting one made this test fail for the right reason three times — the
        # CLK-2 kiosk switch turning shared stations off company-wide, the CLK-4 switch turning spoof
        # review off, and now CLK-1's turning the photo rule off. That is what a tripwire test is for.
        payload={"timezone":"America/Chicago","workweek_start":0,"overtime_after_hours":"40.00",
                 "rounding_mode":"exact","rounding_minutes":1,"require_geofence":"on","allow_kiosk":"on",
                 "flag_spoof_risk":"on","require_selfie":"","allow_reopen":""}
        self.client.post(reverse("time_policy"),payload)
        self.company.refresh_from_db()
        self.assertEqual(self.company.revision,1)
        payload.update({"rounding_mode":"nearest","rounding_minutes":6})
        self.client.post(reverse("time_policy"),payload)
        self.company.refresh_from_db()
        self.assertEqual(self.company.revision,2)


class RecurringSeriesTest(TestCase):
    """A contract stated once as a pattern, then produced as ordinary dated posts.

    The roadmap makes three things this feature's done-criterion: one screen produces N dated
    shifts, a changed credential blocks the row that fails rather than the series, and the posts
    outlive the series that made them.
    """

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User=get_user_model()
        self.owner=User.objects.create_user(username="series-owner@example.com",password="correct horse battery staple")
        self.officer_user=User.objects.create_user(username="series-ready@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Series LLC",display_name="Series",slug="series")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.officer_user,organization=self.org,role=Membership.Role.OFFICER)
        self.officer=Person.objects.create(organization=self.org,user=self.officer_user,first_name="Rosa",last_name="Vega",
            status=Person.Status.ACTIVE,is_unarmed_officer=True,is_commissioned_officer=True,hourly_rate=Decimal("16.00"))
        self.north=Branch.objects.create(organization=self.org,name="North")
        self.south=Branch.objects.create(organization=self.org,name="South")
        self.account=Client.objects.create(organization=self.org,name="Ridgeline Retail",default_pay_rate=Decimal("18.50"))
        self.site=Site.objects.create(organization=self.org,client=self.account,branch=self.north,name="Gate 1",address="1 Gate Rd")
        self.other_site=Site.objects.create(organization=self.org,client=self.account,branch=self.south,name="Warehouse",address="2 Dock St")
        self.commission=CredentialType.objects.create(organization=self.org,name="Commission",code="commission",
            applies_to=["commissioned"],blocks_scheduling=True)
        Credential.objects.create(organization=self.org,person=self.officer,credential_type=self.commission,
            status=Credential.Status.ACTIVE,number="C-1",expires_on=timezone.localdate()+timedelta(days=200))
        self.monday=timezone.localdate()-timedelta(days=timezone.localdate().weekday())+timedelta(days=7)

    def make_series(self,**overrides):
        """Create a series through the real form, so the pattern is what a dispatcher would save."""
        officer=overrides.pop("officer",self.officer)
        days=overrides.pop("days",("0",))
        required=overrides.pop("required",())
        payload={"name":"Gate 1 day tour","site":self.site.pk,
            "officer":officer.pk if officer else "","start_time":"07:00","end_time":"19:00",
            "weekdays":[str(value) for value in days],"pattern":ShiftTemplate.Pattern.WEEKLY,
            "series_start":self.monday.strftime("%Y-%m-%d"),"post_name":"Day tour",
            "post_orders":"Log every arrival.","required_credentials":[str(item.pk) for item in required],"active":"on"}
        payload.update(overrides)
        self.client.force_login(self.owner)
        self.client.post(reverse("shift_template_create"),payload)
        template=ShiftTemplate.objects.filter(name=payload["name"]).first()
        self.assertIsNotNone(template,"the series form refused the payload this test posted")
        return template

    def generate(self,template,start,end,action="preview",status=Shift.Status.PUBLISHED):
        self.client.force_login(self.owner)
        return self.client.post(reverse("shift_template_generate",args=[template.pk]),
            {"range_start":start.strftime("%Y-%m-%d"),"range_end":end.strftime("%Y-%m-%d"),
             "status":status,"action":action})

    def test_one_screen_produces_the_number_of_dated_posts_the_pattern_promises(self):
        from datetime import timedelta
        template=self.make_series()
        self.assertEqual(template.day_indexes,[0])
        preview=self.generate(template,self.monday,self.monday+timedelta(days=20))
        self.assertContains(preview,"Will create")
        self.assertEqual(preview.context["plan"]["count"],3)         # three Mondays
        self.assertEqual(Shift.objects.count(),0)                    # a preview writes nothing
        self.generate(template,self.monday,self.monday+timedelta(days=20),action="generate")
        posts=Shift.objects.filter(template=template).order_by("starts_at")
        self.assertEqual(posts.count(),3)
        first=posts.first()
        self.assertEqual(first.officer_id,self.officer.pk)
        self.assertEqual(first.post_orders,"Log every arrival.")
        self.assertEqual(first.starts_at.astimezone().strftime("%H:%M"),"07:00")
        self.assertTrue(AuditEvent.objects.filter(action="shift_template.generated").exists())

    def test_a_preview_names_the_reason_a_date_is_blocked_instead_of_leaving_a_hole(self):
        from datetime import datetime, time, timedelta
        from django.utils import timezone
        from .models import TimeOffRequest
        template=self.make_series()
        off_day=timezone.make_aware(datetime.combine(self.monday+timedelta(days=14),time(8,0)))
        TimeOffRequest.objects.create(organization=self.org,person=self.officer,starts_at=off_day,
            ends_at=off_day+timedelta(hours=6),status=TimeOffRequest.Status.APPROVED)
        plan=self.generate(template,self.monday,self.monday+timedelta(days=20)).context["plan"]
        blocked=[row for row in plan["rows"] if not row["created"]]
        self.assertEqual(len(blocked),1)
        self.assertIn("approved leave",blocked[0]["reasons"][0])
        self.assertEqual(plan["count"],2)
        self.generate(template,self.monday,self.monday+timedelta(days=20),action="generate")
        self.assertEqual(Shift.objects.filter(template=template).count(),2)

    def test_a_lapsed_credential_blocks_the_rows_the_officer_cannot_stand(self):
        from datetime import timedelta
        from django.utils import timezone
        self.account.required_credentials.add(self.commission)   # imposed by the contract, not the post
        template=self.make_series()
        Credential.objects.filter(person=self.officer).update(expires_on=timezone.localdate()-timedelta(days=1))
        plan=self.generate(template,self.monday,self.monday+timedelta(days=13)).context["plan"]
        self.assertEqual(plan["count"],0)
        self.assertIn("expired",plan["rows"][0]["reasons"][0])
        self.assertIn("Required by the contract",plan["rows"][0]["reasons"][0])

    def test_regenerating_a_range_does_not_double_book_the_post(self):
        from datetime import timedelta
        template=self.make_series()
        self.generate(template,self.monday,self.monday+timedelta(days=13),action="generate")
        again=self.generate(template,self.monday,self.monday+timedelta(days=13)).context["plan"]
        self.assertEqual(again["count"],0)
        self.assertIn("Already generated",again["rows"][0]["reasons"][0])
        self.assertEqual(Shift.objects.filter(template=template).count(),2)

    def test_a_generated_post_keeps_resolving_its_own_rate_instead_of_freezing_the_series_number(self):
        from datetime import timedelta
        from .services import effective_rates
        template=self.make_series()
        self.generate(template,self.monday,self.monday+timedelta(days=6),action="generate")
        post=Shift.objects.get(template=template)
        self.assertIsNone(post.pay_rate)                             # the series never carried a rate
        self.assertEqual(effective_rates(post)["pay_rate"],Decimal("18.50"))
        self.account.default_pay_rate=Decimal("21.00")
        self.account.save(update_fields=["default_pay_rate"])
        resolved=effective_rates(Shift.objects.get(pk=post.pk))   # a fresh read, as any later request is
        self.assertEqual(resolved["pay_rate"],Decimal("21.00"))
        self.assertEqual(resolved["pay_source"],"contract")

    def test_removing_the_series_leaves_the_posts_it_produced_on_the_schedule(self):
        from datetime import timedelta
        template=self.make_series()
        self.generate(template,self.monday,self.monday+timedelta(days=6),action="generate")
        post=Shift.objects.get(template=template)
        self.client.force_login(self.owner)
        self.client.post(reverse("shift_template_remove",args=[template.pk]))
        self.assertFalse(ShiftTemplate.objects.exists())
        post.refresh_from_db()
        self.assertIsNone(post.template_id)                          # SET_NULL, never CASCADE
        self.assertEqual(post.officer_id,self.officer.pk)

    def test_an_overnight_tour_finishes_the_next_morning(self):
        from datetime import timedelta
        template=self.make_series(name="Night tour",start_time="18:00",end_time="06:00",days=(4,))
        self.assertTrue(template.overnight)
        start,end=template.window_for(self.monday+timedelta(days=4))
        self.assertEqual((end-start).total_seconds()/3600,12)
        self.assertEqual(end.astimezone().strftime("%H:%M"),"06:00")
        self.assertEqual(end.astimezone().date(),start.astimezone().date()+timedelta(days=1))

    def test_an_every_other_week_series_keeps_its_phase_whatever_range_is_generated(self):
        from datetime import date, time
        from django.utils import timezone
        template=ShiftTemplate.objects.create(organization=self.org,name="Alternating Saturday",site=self.site,
            start_time=time(7,0),end_time=time(19,0),weekdays=[5],pattern=ShiftTemplate.Pattern.FORTNIGHTLY,
            series_start=date(2026,10,5))
        def saturdays(start,end):
            return sorted({timezone.localtime(row[0]).date() for row in template.occurrences(start,end)})
        self.assertEqual(saturdays(date(2026,10,5),date(2026,11,1)),[date(2026,10,10),date(2026,10,24)])
        self.assertEqual(saturdays(date(2026,10,19),date(2026,11,29)),[date(2026,10,24),date(2026,11,7),date(2026,11,21)])

    def test_the_plan_shows_what_the_series_costs_in_overtime_before_it_is_generated(self):
        from datetime import timedelta
        from .models import TimePolicy
        TimePolicy.objects.create(organization=self.org,overtime_after_hours=Decimal("40.00"))
        template=self.make_series(name="Three tours",days=(0,2,4))
        plan=self.generate(template,self.monday,self.monday+timedelta(days=6)).context["plan"]
        self.assertEqual(len(plan["weeks"]),1)
        self.assertEqual(plan["weeks"][0]["series_hours"],Decimal("36.00"))
        self.assertFalse(plan["weeks"][0]["over"])
        template.officer=None;template.save()
        self.assertEqual(self.generate(template,self.monday,self.monday+timedelta(days=6)).context["plan"]["weeks"],[])

    def test_a_dispatcher_whose_authority_excludes_the_site_cannot_generate_it(self):
        User=get_user_model()
        scoped=User.objects.create_user(username="series-south@example.com",password="correct horse battery staple")
        membership=Membership.objects.create(user=scoped,organization=self.org,role=Membership.Role.SCHEDULER)
        AuthorityScope.objects.create(membership=membership,organization=self.org,branch=self.south)
        template=self.make_series()
        self.client.force_login(scoped)
        self.assertEqual(self.client.get(reverse("shift_template_generate",args=[template.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse("shift_templates")).status_code,200)
        self.assertNotContains(self.client.get(reverse("shift_templates")),"Gate 1 day tour")

    def test_a_series_falling_outside_the_stated_availability_names_the_day(self):
        from datetime import time, timedelta
        AvailabilityRule.objects.create(organization=self.org,person=self.officer,weekday=1,
            starts_at=time(7,0),ends_at=time(19,0))                       # Tuesdays only
        tuesday=self.make_series(name="Tuesday tour",days=(1,))
        self.assertEqual(self.generate(tuesday,self.monday,self.monday+timedelta(days=6)).context["plan"]["notes"],[])
        monday=self.make_series()
        plan=self.generate(monday,self.monday,self.monday+timedelta(days=6)).context["plan"]
        self.assertEqual(plan["count"],1)                                  # still generated: advisory, not a refusal
        self.assertIn("Monday",plan["notes"][0])
        self.assertIn("advisory",plan["notes"][0])

    def test_a_rotating_cycle_produces_the_pattern_the_industry_writes_as_2_2_3(self):
        # A weekday-anchored rule cannot express 2-2-3 at all: it is a 14-day cycle of 2 on, 2 off,
        # 3 on, and "every other weekend off" is the property that makes it the guard rotation.
        # Cycle day 0 is the first day of the series, which here is a Monday.
        from datetime import date
        from django.utils import timezone
        template=self.make_series(name="Panama",pattern=ShiftTemplate.Pattern.CYCLE,
            preset="2-2-3 · Panama · Pitman",weekdays=[],series_start=date(2026,10,5))
        self.assertEqual(template.cycle_days,14)
        self.assertEqual(template.cycle_work_indexes,[0,1,4,5,6,9,10])
        days=sorted({timezone.localtime(row[0]).date() for row in template.occurrences(date(2026,10,5),date(2026,10,18))})
        self.assertEqual(len(days),7)                                   # seven tours a fortnight
        self.assertIn(date(2026,10,10),days)                            # this weekend on
        self.assertIn(date(2026,10,11),days)
        self.assertNotIn(date(2026,10,17),days)                         # the next one off
        self.assertNotIn(date(2026,10,18),days)
        self.assertEqual(template.pattern_label,"Day 1, 2, 5, 6, 7, 10, 11 of 14")

    def test_a_fourteen_day_cycle_is_weekday_locked_and_an_eight_day_one_drifts(self):
        # The distinction the vertical cares about, and the reason weekdays are not the unit: a
        # 14-day cycle is exactly two calendar weeks, while 4-on/4-off walks one weekday forward
        # every round, so no single set of weekdays describes it.
        from datetime import date
        from django.utils import timezone
        locked=self.make_series(name="Panama",pattern=ShiftTemplate.Pattern.CYCLE,
            preset="2-2-3 · Panama · Pitman",weekdays=[],series_start=date(2026,10,5))
        self.assertIn("same weekdays",locked.cycle_note)
        drift=self.make_series(name="Four by four",pattern=ShiftTemplate.Pattern.CYCLE,
            preset="4 on · 4 off",weekdays=[],series_start=date(2026,10,5))
        self.assertIn("move 1 weekday forward",drift.cycle_note)
        first=[timezone.localtime(row[0]).date() for row in drift.occurrences(date(2026,10,5),date(2026,10,12))]
        self.assertEqual([day.strftime("%a") for day in first],["Mon","Tue","Wed","Thu"])
        second=[timezone.localtime(row[0]).date() for row in drift.occurrences(date(2026,10,13),date(2026,10,20))]
        self.assertEqual([day.strftime("%a") for day in second],["Tue","Wed","Thu","Fri"])

    def test_one_cycle_offset_by_days_is_how_two_crews_stand_a_post_without_a_gap(self):
        # What a recurring-pattern feature is actually for: 4-on/4-off on one post is two series on
        # the same cycle, the second starting four days later. Asserted as coverage rather than as
        # dates, because "every day has exactly one post on it" is the thing a dispatcher checks.
        from datetime import date, time, timedelta
        from django.utils import timezone
        crew_a=self.make_series(name="Crew A",pattern=ShiftTemplate.Pattern.CYCLE,
            preset="4 on · 4 off",weekdays=[],series_start=date(2026,10,5))
        crew_b=ShiftTemplate.objects.create(organization=self.org,name="Crew B",site=self.site,
            start_time=time(7,0),end_time=time(19,0),pattern=ShiftTemplate.Pattern.CYCLE,
            cycle_days=8,cycle_work_days=[0,1,2,3],series_start=date(2026,10,9),weekdays=[])
        days=[date(2026,10,5)+timedelta(days=index) for index in range(8)]
        covered=([timezone.localtime(row[0]).date() for row in crew_a.occurrences(days[0],days[-1])]
                 +[timezone.localtime(row[0]).date() for row in crew_b.occurrences(days[0],days[-1])])
        self.assertEqual(sorted(covered),sorted(days))                  # every day, exactly once

    def test_a_series_end_date_bounds_generation_and_gives_one_click_to_the_end(self):
        from datetime import date
        from django.utils import timezone
        template=self.make_series(name="Until the 20th",pattern=ShiftTemplate.Pattern.CYCLE,
            preset="4 on · 4 off",weekdays=[],series_start=date(2026,10,5),series_end=date(2026,10,20))
        self.assertEqual(template.term_label,"05 Oct 2026 – 20 Oct 2026")
        late=[timezone.localtime(row[0]).date() for row in template.occurrences(date(2026,10,1),date(2026,11,30))]
        self.assertTrue(late)
        self.assertLessEqual(max(late),date(2026,10,20))                # clamped in the model, not per caller
        self.assertIsNone(template.next_window(date(2026,10,21)))       # nothing left to generate
        self.assertEqual(template.next_window(date(2026,10,6)),(date(2026,10,6),date(2026,10,20)))

    def test_next_window_stays_filled_to_a_horizon_instead_of_running_ahead_forever(self):
        # "Keep this filled four weeks ahead" has to mean today + 28, not start + 28. Measured from
        # the start, an unattended pass would extend the roster a little further on every run.
        from datetime import datetime, timedelta
        from django.utils import timezone
        template=self.make_series()
        self.assertEqual(template.next_window(self.monday,horizon=28),(self.monday,self.monday+timedelta(days=28)))
        edge=timezone.make_aware(datetime.combine(self.monday+timedelta(days=28),datetime.min.time()))
        Shift.objects.create(organization=self.org,site=self.site,starts_at=edge,ends_at=edge+timedelta(hours=12),
            status=Shift.Status.PUBLISHED,officer=self.officer,template=template)
        self.assertEqual(template.last_generated_on,self.monday+timedelta(days=28))
        self.assertIsNone(template.next_window(self.monday,horizon=28))  # the horizon is already covered

    def test_an_unattended_run_only_touches_series_that_were_opted_into(self):
        from .services import fill_active_series
        opted=self.make_series(name="Standing post",days=(0,1,2,3,4,5,6))
        ShiftTemplate.objects.filter(pk=opted.pk).update(auto_generate_days=14)
        self.make_series(name="Left alone",post_name="Manual only")
        created,blocked=fill_active_series(self.org)
        self.assertGreater(created,0)
        self.assertEqual(blocked,0)
        self.assertEqual(Shift.objects.filter(template=opted).count(),created)
        self.assertEqual(Shift.objects.filter(template__name="Left alone").count(),0)
        self.assertEqual(fill_active_series(self.org),(0,0))             # idempotent: the horizon is covered

    def test_an_unattended_run_reports_the_dates_it_could_not_place(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Notification
        from .services import fill_active_series
        template=self.make_series(required=[self.commission])
        ShiftTemplate.objects.filter(pk=template.pk).update(auto_generate_days=14)
        Credential.objects.filter(person=self.officer).update(expires_on=timezone.localdate()-timedelta(days=1))
        created,blocked=fill_active_series(self.org)
        self.assertEqual(created,0)
        self.assertGreater(blocked,0)
        notice=Notification.objects.filter(event_type="shift.series_blocked").first()
        self.assertIsNotNone(notice)                                     # the hole is shown, not swallowed
        self.assertIn("expired",notice.body)
        self.assertIn("Gate 1",notice.subject)

    def test_the_series_lists_on_its_page_and_its_posts_on_the_officers_own(self):
        from datetime import timedelta
        template=self.make_series()
        self.client.force_login(self.owner)
        page=self.client.get(reverse("shift_templates"))
        self.assertContains(page,"Gate 1 day tour")
        self.assertContains(page,"Monday")
        self.generate(template,self.monday,self.monday+timedelta(days=6),action="generate")
        self.client.force_login(self.officer_user)
        mine=self.client.get(reverse("my_shifts"))
        self.assertContains(mine,"Gate 1")
        self.assertContains(mine,"Move this post")
        self.assertContains(mine,"Day tour")


class ShiftSwapTest(TestCase):
    """A filled post that its own officer offers to a colleague.

    The asymmetry the peers all keep: an unfilled post is broadcast and taken, a filled one moves
    only with both people plus a manager. The tests below hold that shape, and the one property
    that makes it safe to move an assignment at all — recorded time never changes owner.
    """

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User=get_user_model()
        self.owner=User.objects.create_user(username="swap-owner@example.com",password="correct horse battery staple")
        self.rosa_user=User.objects.create_user(username="swap-rosa@example.com",password="correct horse battery staple")
        self.sam_user=User.objects.create_user(username="swap-sam@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Swap LLC",display_name="Swap",slug="swap")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        for user in (self.rosa_user,self.sam_user):
            Membership.objects.create(user=user,organization=self.org,role=Membership.Role.OFFICER)
        self.rosa=Person.objects.create(organization=self.org,user=self.rosa_user,first_name="Rosa",last_name="Vega",
            status=Person.Status.ACTIVE,is_commissioned_officer=True)
        self.sam=Person.objects.create(organization=self.org,user=self.sam_user,first_name="Sam",last_name="Okafor",
            status=Person.Status.ACTIVE,is_commissioned_officer=True)
        self.north=Branch.objects.create(organization=self.org,name="North")
        self.south=Branch.objects.create(organization=self.org,name="South")
        self.account=Client.objects.create(organization=self.org,name="Ridgeline Retail")
        self.site=Site.objects.create(organization=self.org,client=self.account,branch=self.north,name="Gate 1",address="1 Gate Rd")
        self.commission=CredentialType.objects.create(organization=self.org,name="Commission",code="commission",
            applies_to=["commissioned"],blocks_scheduling=True)
        for person in (self.rosa,self.sam):
            Credential.objects.create(organization=self.org,person=person,credential_type=self.commission,
                status=Credential.Status.ACTIVE,number=f"C-{person.pk.hex[:6]}",expires_on=timezone.localdate()+timedelta(days=120))
        now=timezone.now()
        self.post=Shift.objects.create(organization=self.org,site=self.site,starts_at=now+timedelta(days=2),
            ends_at=now+timedelta(days=2,hours=12),status=Shift.Status.PUBLISHED,officer=self.rosa,post_name="Armed gate")
        self.post.required_credentials.add(self.commission)

    def offer(self,shift=None,replacement=None,note="school run",mode="handoff"):
        self.client.force_login(self.rosa_user)
        payload={"note":note,"mode":mode}
        payload["replacement" if mode=="handoff" else "partner"]=(replacement or self.sam).pk
        return self.client.post(reverse("offer_post",args=[(shift or self.post).pk]),payload)

    def accept(self,swap,as_user=None):
        self.client.force_login(as_user or self.sam_user)
        return self.client.post(reverse("swap_respond",args=[swap.pk]),{"action":ShiftSwap.Status.AGREED})

    def decide(self,swap,action=ShiftSwap.Status.APPROVED,note=""):
        self.client.force_login(self.owner)
        return self.client.post(reverse("swap_decide",args=[swap.pk]),{"action":action,"note":note})

    def test_a_filled_post_moves_only_with_both_officers_and_a_manager(self):
        from .models import Notification
        response=self.offer()
        self.assertRedirects(response,reverse("my_shifts"))
        swap=ShiftSwap.objects.get(shift=self.post)
        self.assertEqual(swap.status,ShiftSwap.Status.OFFERED)
        self.assertEqual(swap.requester_id,self.rosa.pk)
        self.post.refresh_from_db()
        self.assertEqual(self.post.officer_id,self.rosa.pk)          # an offer alone moves nothing
        self.assertTrue(Notification.objects.filter(recipient=self.sam_user,event_type="shift.swap_offered").exists())
        self.assertFalse(Notification.objects.filter(event_type="shift.swap_agreed").exists())
        self.accept(swap)
        swap.refresh_from_db()
        self.assertEqual(swap.status,ShiftSwap.Status.AGREED)
        self.assertIsNotNone(swap.agreed_at)
        self.post.refresh_from_db()
        self.assertEqual(self.post.officer_id,self.rosa.pk)          # still not moved until a manager decides
        queue=self.client.get(reverse("swaps"))                      # an officer has no approval surface here
        self.assertEqual(queue.status_code,403)
        self.client.force_login(self.owner)
        queue=self.client.get(reverse("swaps"))
        self.assertContains(queue,"Sam Okafor")
        self.assertContains(queue,"Both agreed")
        self.decide(swap)
        swap.refresh_from_db(); self.post.refresh_from_db()
        self.assertEqual(swap.status,ShiftSwap.Status.APPROVED)
        self.assertEqual(self.post.officer_id,self.sam.pk)
        self.assertTrue(Notification.objects.filter(recipient=self.sam_user,event_type="shift.swap_approved").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.rosa_user,event_type="shift.swap_approved").exists())
        self.assertTrue(AuditEvent.objects.filter(action="shift.swap_approved").exists())

    def test_a_declined_offer_leaves_the_post_with_the_officer_who_stood_it(self):
        self.client.force_login(self.rosa_user)
        self.offer()
        swap=ShiftSwap.objects.get(shift=self.post)
        self.client.force_login(self.sam_user)
        self.client.post(reverse("swap_respond",args=[swap.pk]),{"action":ShiftSwap.Status.DECLINED})
        swap.refresh_from_db(); self.post.refresh_from_db()
        self.assertEqual(swap.status,ShiftSwap.Status.DECLINED)
        self.assertEqual(self.post.officer_id,self.rosa.pk)
        self.assertTrue(Notification.objects.filter(recipient=self.rosa_user,event_type="shift.swap_declined").exists())

    def test_the_qualification_check_runs_at_approval_not_at_the_offer(self):
        from datetime import timedelta
        from django.utils import timezone
        self.offer()
        swap=ShiftSwap.objects.get(shift=self.post)
        self.accept(swap)
        Credential.objects.filter(person=self.sam).update(expires_on=timezone.localdate()-timedelta(days=1))
        self.decide(swap)
        swap.refresh_from_db(); self.post.refresh_from_db()
        self.assertEqual(self.post.officer_id,self.rosa.pk)          # refused: the post keeps a qualified officer
        self.assertEqual(swap.status,ShiftSwap.Status.AGREED)        # and stays open for the manager to see why
        queue=self.client.get(reverse("swaps"))
        self.assertContains(queue,"expired")                          # the reason is on the screen, not just the log

    def test_someone_who_could_not_take_the_post_is_not_offerable_and_says_why(self):
        # Peers filter the candidate list at offer time (Deputy, Connecteam, When I Work) and still
        # re-check at approval. The filter must be legible, not a silently short list: an officer
        # who finds a colleague missing needs to see that it is the licence, not a bug.
        from datetime import timedelta
        from django.utils import timezone
        Credential.objects.filter(person=self.sam).update(expires_on=timezone.localdate()-timedelta(days=1))
        response=self.offer()
        self.assertEqual(response.status_code,200)                    # re-rendered, nothing created
        self.assertFalse(ShiftSwap.objects.exists())
        page=self.client.get(reverse("offer_post",args=[self.post.pk]))
        self.assertContains(page,"Sam Okafor")
        self.assertContains(page,"is missing a valid Commission")

    def test_recorded_time_stays_on_the_officer_who_actually_worked_it(self):
        from datetime import timedelta
        from django.utils import timezone
        from .services import payroll_rows
        in_at=self.post.starts_at
        for kind,when in ((Punch.Kind.IN,in_at),(Punch.Kind.OUT,in_at+timedelta(hours=12))):
            Punch.objects.create(organization=self.org,person=self.rosa,shift=self.post,kind=kind,
                occurred_at=when,client_event_id=uuid.uuid4())
        self.offer(); swap=ShiftSwap.objects.get(shift=self.post); self.accept(swap); self.decide(swap)
        self.post.refresh_from_db()
        self.assertEqual(self.post.officer_id,self.sam.pk)
        self.assertEqual(Punch.objects.filter(shift=self.post,person=self.rosa).count(),2)   # untouched
        rows=payroll_rows(self.org,in_at-timedelta(hours=1),in_at+timedelta(days=2))
        names={row["employee"] for row in rows}
        self.assertIn("Rosa Vega",names)
        self.assertNotIn("Sam Okafor",names)                          # scheduled is not the same as worked

    def test_the_notice_reaches_the_supervisor_whose_authority_covers_the_post(self):
        User=get_user_model()
        covering=User.objects.create_user(username="swap-north@example.com",password="correct horse battery staple")
        distant=User.objects.create_user(username="swap-south@example.com",password="correct horse battery staple")
        for user,branch in ((covering,self.north),(distant,self.south)):
            membership=Membership.objects.create(user=user,organization=self.org,role=Membership.Role.SUPERVISOR)
            AuthorityScope.objects.create(membership=membership,organization=self.org,branch=branch)
        self.offer(); swap=ShiftSwap.objects.get(shift=self.post)
        self.client.force_login(covering)
        self.assertEqual(self.client.get(reverse("swaps")).status_code,200)
        self.accept(swap)
        recipients=set(Notification.objects.filter(event_type="shift.swap_agreed").values_list("recipient_id",flat=True))
        self.assertIn(covering.pk,recipients)
        self.assertNotIn(distant.pk,recipients)                        # not their branch, not their decision
        self.assertIn(self.owner.pk,recipients)

    def test_a_supervisor_whose_authority_excludes_the_post_cannot_decide_it(self):
        User=get_user_model()
        distant=User.objects.create_user(username="swap-far@example.com",password="correct horse battery staple")
        membership=Membership.objects.create(user=distant,organization=self.org,role=Membership.Role.SUPERVISOR)
        AuthorityScope.objects.create(membership=membership,organization=self.org,branch=self.south)
        self.offer(); swap=ShiftSwap.objects.get(shift=self.post); self.accept(swap)
        self.client.force_login(distant)
        self.assertEqual(self.client.get(reverse("swaps")).status_code,200)
        self.assertNotContains(self.client.get(reverse("swaps")),"Armed gate")
        self.assertEqual(self.client.post(reverse("swap_decide",args=[swap.pk]),
            {"action":ShiftSwap.Status.APPROVED,"note":""}).status_code,404)

    def test_a_manager_cannot_approve_before_the_colleague_has_answered(self):
        self.offer(); swap=ShiftSwap.objects.get(shift=self.post)
        self.decide(swap)
        swap.refresh_from_db(); self.post.refresh_from_db()
        self.assertEqual(swap.status,ShiftSwap.Status.OFFERED)
        self.assertEqual(self.post.officer_id,self.rosa.pk)

    def test_approval_is_refused_when_the_post_moved_in_the_meantime(self):
        from datetime import timedelta
        from django.utils import timezone
        third_user=get_user_model().objects.create_user(username="swap-third@example.com",password="correct horse battery staple")
        Membership.objects.create(user=third_user,organization=self.org,role=Membership.Role.OFFICER)
        third=Person.objects.create(organization=self.org,user=third_user,first_name="Lee",last_name="Ashford",
            status=Person.Status.ACTIVE,is_commissioned_officer=True)
        Credential.objects.create(organization=self.org,person=third,credential_type=self.commission,
            status=Credential.Status.ACTIVE,number="C-3",expires_on=timezone.localdate()+timedelta(days=60))
        self.offer(); swap=ShiftSwap.objects.get(shift=self.post); self.accept(swap)
        self.post.officer=third; self.post.save(update_fields=["officer"])     # a dispatcher refilled it
        self.decide(swap)
        swap.refresh_from_db(); self.post.refresh_from_db()
        self.assertEqual(self.post.officer_id,third.pk)
        self.assertEqual(swap.status,ShiftSwap.Status.REFUSED)
        self.assertIn("reassigned",swap.review_note)

    def test_the_overview_counts_the_same_offers_the_queue_lists(self):
        self.offer(); swap=ShiftSwap.objects.get(shift=self.post)
        self.accept(swap)
        self.client.force_login(self.owner)
        overview=self.client.get(reverse("dashboard"))
        self.assertEqual(overview.context["pending_move_count"],1)
        self.assertEqual(overview.context["pending_move_count"],len(self.client.get(reverse("swaps")).context["pending"]))
        closed=ShiftSwap.objects.get(pk=swap.pk); closed.status=ShiftSwap.Status.WITHDRAWN; closed.save()
        self.assertEqual(self.client.get(reverse("dashboard")).context["pending_move_count"],0)

    def test_a_pending_offer_shows_on_the_officers_overview(self):
        self.offer()
        self.client.force_login(self.sam_user)
        self.assertContains(self.client.get(reverse("dashboard")),"1 hand-off or trade offer waiting on you")

    def test_only_the_officer_standing_the_post_can_offer_it(self):
        self.client.force_login(self.sam_user)
        response=self.client.post(reverse("offer_post",args=[self.post.pk]),{"replacement":self.rosa.pk,"note":""})
        self.assertEqual(response.status_code,404)
        self.assertFalse(ShiftSwap.objects.exists())

    def test_one_offer_is_in_flight_per_post_and_the_officer_can_withdraw_it(self):
        from datetime import timedelta
        from django.utils import timezone
        User=get_user_model()
        self.offer()
        swap=ShiftSwap.objects.get(shift=self.post)
        self.offer(replacement=self.sam)                                # same colleague, duplicate
        self.assertEqual(ShiftSwap.objects.filter(shift=self.post).count(),1)
        other_user=User.objects.create_user(username="swap-nia@example.com",password="correct horse battery staple")
        Membership.objects.create(user=other_user,organization=self.org,role=Membership.Role.OFFICER)
        other=Person.objects.create(organization=self.org,user=other_user,first_name="Nia",last_name="Rowe",
            status=Person.Status.ACTIVE,is_commissioned_officer=True)
        Credential.objects.create(organization=self.org,person=other,credential_type=self.commission,
            status=Credential.Status.ACTIVE,number="C-NIA",expires_on=timezone.localdate()+timedelta(days=60))
        self.client.force_login(self.rosa_user)
        self.client.post(reverse("offer_post",args=[self.post.pk]),
            {"replacement":other.pk,"note":"second try","mode":"handoff"})
        self.assertEqual(ShiftSwap.objects.filter(shift=self.post).count(),1)   # a second colleague is refused too
        self.client.post(reverse("swap_withdraw",args=[swap.pk]))
        swap.refresh_from_db()
        self.assertEqual(swap.status,ShiftSwap.Status.WITHDRAWN)
        self.client.post(reverse("offer_post",args=[self.post.pk]),
            {"replacement":other.pk,"note":"after withdrawing","mode":"handoff"})
        self.assertEqual(ShiftSwap.objects.filter(shift=self.post).count(),2)
        self.client.post(reverse("swap_withdraw",args=[swap.pk]))       # already closed: no 500, no change

    def test_approving_one_offer_closes_the_other_open_offers_on_that_post(self):
        self.offer(); swap=ShiftSwap.objects.get(shift=self.post)
        second=Person.objects.create(organization=self.org,first_name="Sam",last_name="Second",
            status=Person.Status.ACTIVE,is_commissioned_officer=True)
        # A second open offer exists only because a dispatcher or an import can make one; the route
        # itself refuses it. Approval has to close it rather than leave a manager deciding a
        # post that is already filled.
        live=ShiftSwap.objects.create(organization=self.org,shift=self.post,requester=self.rosa,replacement=second,
            status=ShiftSwap.Status.OFFERED)
        self.accept(swap); self.decide(swap)
        live.refresh_from_db()
        self.assertEqual(live.status,ShiftSwap.Status.REFUSED)
        self.assertIn("Another offer",live.review_note)

    def test_the_post_appears_on_the_officers_own_page_with_the_offer_in_motion(self):
        self.client.force_login(self.rosa_user)
        page=self.client.get(reverse("my_shifts"))
        self.assertContains(page,"Armed gate")
        self.assertContains(page,"Move this post")
        self.offer()
        page=self.client.get(reverse("my_shifts"))
        self.assertContains(page,"waiting for them")
        self.assertNotContains(page,"Move this post")                  # not offerable twice
        self.client.force_login(self.sam_user)
        incoming=self.client.get(reverse("my_shifts"))
        self.assertContains(incoming,"Rosa Vega")
        self.assertContains(incoming,"You qualify now")
        self.assertContains(incoming,"Accept")

    def test_a_colleague_without_a_sign_in_is_excluded_with_the_reason(self):
        from datetime import timedelta
        from django.utils import timezone
        no_user=Person.objects.create(organization=self.org,first_name="Owen",last_name="Blake",
            status=Person.Status.ACTIVE,is_commissioned_officer=True)
        Credential.objects.create(organization=self.org,person=no_user,credential_type=self.commission,
            status=Credential.Status.ACTIVE,number="C-9",expires_on=timezone.localdate()+timedelta(days=30))
        self.client.force_login(self.rosa_user)
        response=self.client.post(reverse("offer_post",args=[self.post.pk]),
            {"replacement":no_user.pk,"note":"cover?","mode":"handoff"})
        self.assertEqual(response.status_code,200)                   # not offeredable, and not a 500
        self.assertFalse(ShiftSwap.objects.exists())
        self.assertContains(self.client.get(reverse("offer_post",args=[self.post.pk])),"has no sign-in")

    def test_an_inactive_colleague_is_not_a_choice_the_form_will_take(self):
        retired=Person.objects.create(organization=self.org,first_name="Ed",last_name="Stone",
            status=Person.Status.INACTIVE,is_commissioned_officer=True)
        self.client.force_login(self.rosa_user)
        response=self.client.post(reverse("offer_post",args=[self.post.pk]),{"replacement":retired.pk,"note":"","mode":"handoff"})
        self.assertEqual(response.status_code,200)                     # re-rendered with the error, not created
        self.assertFalse(ShiftSwap.objects.exists())

    def test_a_completed_post_is_not_theirs_to_offer(self):
        from datetime import timedelta
        from django.utils import timezone
        past=Shift.objects.create(organization=self.org,site=self.site,starts_at=timezone.now()-timedelta(days=3),
            ends_at=timezone.now()-timedelta(days=2),status=Shift.Status.COMPLETED,officer=self.rosa,post_name="Old gate")
        self.client.force_login(self.rosa_user)
        response=self.client.post(reverse("offer_post",args=[past.pk]),{"replacement":self.sam.pk,"note":""})
        self.assertRedirects(response,reverse("my_shifts"))
        self.assertFalse(ShiftSwap.objects.exists())


class RuleHistoryTest(TestCase):
    """A rule's saved versions survive the rule itself.

    POL-1's done-criterion is specific and this class exists to meet it: a punch stamped ``4:2``
    must still say what version 2 was after the row that produced it has been deleted. CMP-2 is
    the same defect in the compliance matrix — change a reminder lead time and yesterday's
    evaluation was computed from text that no longer exists — so it rides on the one mechanism.
    """

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User=get_user_model()
        self.owner=User.objects.create_user(username="history-owner@example.com",password="correct horse battery staple")
        self.guard_user=User.objects.create_user(username="history-guard@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="History LLC",display_name="History",slug="history")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.guard_user,organization=self.org,role=Membership.Role.OFFICER)
        self.guard=Person.objects.create(organization=self.org,user=self.guard_user,first_name="Nadia",
            last_name="Petrov",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        self.account=Client.objects.create(organization=self.org,name="Depot")
        self.site=Site.objects.create(organization=self.org,client=self.account,name="Yard",address="1 Yard")
        self.policy=TimePolicy.objects.create(organization=self.org,rounding_mode=TimePolicy.RoundingMode.EXACT,
            rounding_minutes=1)
        self.now=timezone.now()

    def add_rule(self,site=None,geofence="no",mode="nearest",minutes="15"):
        self.client.force_login(self.owner)
        self.client.post(reverse("time_policy_override_create"),{"client":"","site":(site or self.site).pk,
            "require_geofence":geofence,"rounding_mode":mode,"rounding_minutes":minutes})
        return TimePolicyOverride.objects.filter(organization=self.org).first()

    def test_a_stamp_taken_before_the_rule_was_deleted_still_resolves_to_its_own_numbers(self):
        from .services import record_punch, resolve_rule_version
        rule=self.add_rule()
        self.assertEqual(rule.revision,1)
        record_punch(organization=self.org,person=self.guard,client_event_id=uuid.uuid4(),kind="in",
            occurred_at=self.now,actor=self.owner,site=self.site,latitude=Decimal("32.77"),longitude=Decimal("-96.80"))
        stamp=AuditEvent.objects.filter(action="punch.recorded").latest("id").metadata["policy_version"]
        self.assertEqual(stamp,f"{rule.pk}:1")
        self.client.post(reverse("time_policy_override",args=[rule.pk]),{"client":"","site":self.site.pk,
            "require_geofence":"no","rounding_mode":"nearest","rounding_minutes":"30"})
        rule.refresh_from_db()
        self.assertEqual(rule.revision,2)
        resolved=resolve_rule_version(self.org,"site",stamp)
        self.assertEqual(resolved["values"]["rounding_minutes"],15)      # what version 1 actually said
        self.assertEqual(resolved["values"]["require_geofence"],False)
        self.client.post(reverse("time_policy_override_remove",args=[rule.pk]))
        self.assertFalse(TimePolicyOverride.objects.filter(pk=rule.pk).exists())
        after=resolve_rule_version(self.org,"site",stamp)
        self.assertEqual(after["values"],resolved["values"])             # the deletion changed nothing
        self.assertEqual(after["label"],"Depot — Yard")

    def test_version_two_is_a_different_answer_not_an_updated_copy_of_version_one(self):
        from .services import resolve_rule_version
        rule=self.add_rule()
        self.client.post(reverse("time_policy_override",args=[rule.pk]),{"client":"","site":self.site.pk,
            "require_geofence":"yes","rounding_mode":"nearest","rounding_minutes":"30"})
        rule.refresh_from_db()
        one=resolve_rule_version(self.org,"site",f"{rule.pk}:1")
        two=resolve_rule_version(self.org,"site",f"{rule.pk}:2")
        self.assertEqual(one["values"]["require_geofence"],False)
        self.assertEqual(two["values"]["require_geofence"],True)
        self.assertEqual(two["changed"]["require_geofence"],{"before":False,"after":True})

    def test_saving_a_rule_that_changed_nothing_does_not_manufacture_a_version(self):
        rule=self.add_rule()
        self.client.force_login(self.owner)
        self.client.post(reverse("time_policy_override",args=[rule.pk]),{"client":"","site":self.site.pk,
            "require_geofence":"no","rounding_mode":"nearest","rounding_minutes":"15"})
        rule.refresh_from_db()
        self.assertEqual(rule.revision,1)
        self.assertEqual(RuleRevision.objects.filter(kind=RuleRevision.Kind.CLOCK_RULE).count(),1)

    def test_the_company_baseline_is_versioned_the_same_way(self):
        from .services import resolve_rule_version
        self.client.force_login(self.owner)
        payload={"timezone":"America/Chicago","workweek_start":0,"overtime_after_hours":"40.00",
                 "rounding_mode":"nearest","rounding_minutes":"6","require_geofence":"on","allow_reopen":""}
        self.client.post(reverse("time_policy"),payload)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.revision,2)
        resolved=resolve_rule_version(self.org,"company",f"{self.policy.pk}:1")
        self.assertEqual(resolved["values"]["rounding_minutes"],1)
        self.assertEqual(resolved["values"]["rounding_mode"],TimePolicy.RoundingMode.EXACT)
        self.assertEqual(resolve_rule_version(self.org,"company",f"{self.policy.pk}:2")["values"]["rounding_minutes"],6)

    def test_a_requirement_that_changes_a_lead_time_gets_a_new_version(self):
        from .services import record_rule_revision
        # CMP-2: an evaluation and a reminder sent against a 60-day lead time cannot be reproduced
        # once somebody edits it to 30, unless the 60 is still on record somewhere.
        rule=CredentialType.objects.create(organization=self.org,name="Guard registration",code="guard-reg",
            applies_to=["unarmed"],warning_days=60,reminder_days_before=[30,7])
        record_rule_revision(rule,RuleRevision.Kind.CREDENTIAL_RULE)
        self.client.force_login(self.owner)
        self.client.post(reverse("credential_type_edit",args=[rule.pk]),{"name":"Guard registration","code":"guard-reg",
            "jurisdiction":"Texas","authority_url":"https://www.dps.texas.gov/section/private-security",
            "authority_reference":"37 TAC 35.22","interpretation":"Registration must be current to stand a post.",
            "effective_from":"","effective_until":"","blocks_scheduling":"on","blocks_clock_in":"on",
            "warning_days":30,"reminder_days_before":"7","evidence_required":"on","applies_to":["unarmed"],"active":"on"})
        rule.refresh_from_db()
        self.assertEqual(rule.revision,2)
        one=RuleRevision.objects.get(kind=RuleRevision.Kind.CREDENTIAL_RULE,rule_id=str(rule.pk),revision=1)
        self.assertEqual(one.values["warning_days"],60)
        self.assertEqual(one.values["reminder_days_before"],[30,7])
        two=RuleRevision.objects.get(kind=RuleRevision.Kind.CREDENTIAL_RULE,rule_id=str(rule.pk),revision=2)
        self.assertEqual(two.changed["warning_days"],{"before":60,"after":30})

    def test_renaming_a_requirement_is_not_a_new_version_of_the_rule(self):
        from .services import record_rule_revision
        # `name` and `code` are not watched, so a rename must not move the revision: an evaluation
        # cannot come out differently because the label was retyped, and a version number that moves
        # for nothing stops meaning anything. The old name still survives, in the row's label.
        rule=CredentialType.objects.create(organization=self.org,name="Guard registration",code="guard-reg",
            applies_to=["unarmed"],warning_days=60,authority_url="https://www.dps.texas.gov/section/private-security",
            authority_reference="37 TAC 35.22",interpretation="Current registration required.",evidence_required=True)
        record_rule_revision(rule,RuleRevision.Kind.CREDENTIAL_RULE)
        self.client.force_login(self.owner)
        # Only `name` differs from what was on file; every watched value is posted back unchanged.
        self.client.post(reverse("credential_type_edit",args=[rule.pk]),{"name":"NSD registration","code":"guard-reg",
            "jurisdiction":"Texas","authority_url":"https://www.dps.texas.gov/section/private-security",
            "authority_reference":"37 TAC 35.22","interpretation":"Current registration required.",
            "effective_from":"","effective_until":"","blocks_scheduling":"on","blocks_clock_in":"on",
            "warning_days":60,"reminder_days_before":"","evidence_required":"on","applies_to":["unarmed"],"active":"on"})
        rule.refresh_from_db()
        self.assertEqual(rule.revision,1)

    def test_rules_that_predate_the_history_table_are_seeded_once(self):
        from .services import ensure_rule_history
        rule=self.add_rule()
        CredentialType.objects.create(organization=self.org,name="Commission",code="commission",applies_to=["unarmed"])
        RuleRevision.objects.all().delete()
        self.assertEqual(ensure_rule_history(self.org),3)               # baseline, this rule, the requirement
        self.assertEqual(ensure_rule_history(self.org),0)               # idempotent

    def test_a_stamp_with_no_matching_version_is_reported_as_unresolvable(self):
        from .services import resolve_rule_version
        rule=self.add_rule()
        self.assertIsNone(resolve_rule_version(self.org,"site",f"{rule.pk}:9"))   # never existed
        self.assertIsNone(resolve_rule_version(self.org,"site","not-a-stamp"))
        self.assertIsNone(resolve_rule_version(self.org,"nonsense","1:1"))         # unknown level

    def test_the_history_page_orders_versions_and_names_what_changed(self):
        rule=self.add_rule()
        self.client.post(reverse("time_policy_override",args=[rule.pk]),{"client":"","site":self.site.pk,
            "require_geofence":"yes","rounding_mode":"nearest","rounding_minutes":"30"})
        self.client.force_login(self.owner)
        page=self.client.get(reverse("rule_history"))
        self.assertContains(page,"Depot — Yard")
        self.assertContains(page,"Contract or site clock rule")
        self.assertContains(page,"v2")
        self.assertContains(page,"rounding_minutes")
        filtered=self.client.get(reverse("rule_history_rule",args=[RuleRevision.Kind.CLOCK_RULE,str(rule.pk)]))
        self.assertContains(filtered,"v1")
        self.assertEqual(len(filtered.context["revisions"]),2)
        self.assertEqual(self.client.get(reverse("rule_history_kind",args=["credential_rule"])).status_code,200)
        self.assertEqual(self.client.get(reverse("rule_history_kind",args=["invented"])).status_code,404)

    def test_only_privileged_roles_may_read_the_rule_history(self):
        self.add_rule()
        self.client.force_login(self.guard_user)
        self.assertEqual(self.client.get(reverse("rule_history")).status_code,403)


class PayCodeAndLeaveExportTest(TestCase):
    """The two columns a payroll clerk cannot work without.

    PAY-1 is the job code the receiving system keys an hour on — without it the export needs a
    manual mapping step every period. PAY-3 is approved leave appearing as its own category rather
    than as a hole in worked time. Both are asserted at the export boundary, because that is the
    surface the customer actually consumes.
    """

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User=get_user_model()
        self.owner=User.objects.create_user(username="code-owner@example.com",password="correct horse battery staple")
        self.payroll_user=User.objects.create_user(username="code-payroll@example.com",password="correct horse battery staple")
        self.guard_user=User.objects.create_user(username="code-guard@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Codes LLC",display_name="Codes",slug="codes")
        self.other=Organization.objects.create(legal_name="Other LLC",display_name="Other",slug="other")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.payroll_user,organization=self.org,role=Membership.Role.PAYROLL)
        Membership.objects.create(user=self.guard_user,organization=self.org,role=Membership.Role.OFFICER)
        self.guard=Person.objects.create(organization=self.org,user=self.guard_user,first_name="Rosa",
            last_name="Vega",status=Person.Status.ACTIVE,is_unarmed_officer=True,hourly_rate=Decimal("15.00"))
        self.account=Client.objects.create(organization=self.org,name="Ridgeline",default_pay_rate=Decimal("20.00"))
        self.site=Site.objects.create(organization=self.org,client=self.account,name="Gate 1",address="1 Gate Rd")
        self.now=timezone.now()
        # Two posts on purpose. The clock refuses a punch ahead of the server and refuses an offline
        # punch older than its 12-hour sync window, so time that can be worked has to sit inside
        # that band; the schedule page shows the coming week, so the post it must display has to be
        # in the future. Sharing one row between those two tests cannot work.
        self.start=self.now-timedelta(hours=11); self.end=self.now-timedelta(hours=1)
        self.worked_post=Shift.objects.create(organization=self.org,site=self.site,officer=self.guard,
            starts_at=self.start,ends_at=self.end,status=Shift.Status.COMPLETED,post_name="Worked twelves")
        self.period_start=self.start-timedelta(hours=1); self.period_end=self.end+timedelta(hours=1)
        self.post=Shift.objects.create(organization=self.org,site=self.site,officer=self.guard,
            starts_at=self.now+timedelta(days=1),ends_at=self.now+timedelta(days=1,hours=12),
            status=Shift.Status.PUBLISHED,post_name="Gate twelves")
        self.patrol=PayCode.objects.create(organization=self.org,name="Patrol post",code="SEC-PATROL",cost_centre="4102")
        self.courier=PayCode.objects.create(organization=self.org,name="Courier duty",code="SEC-COURIER")
        self.foreign=PayCode.objects.create(organization=self.other,name="Not ours",code="NOPE")

    def worked(self):
        from .services import payroll_rows
        # Punches are written directly rather than through record_punch: this class is about which
        # columns reach the customer's payroll file, and routing them through the clock would make
        # every assertion here depend on the geofence and sync-window gates as well.
        for kind, when in ((Punch.Kind.IN, self.start),(Punch.Kind.OUT, self.end)):
            Punch.objects.create(organization=self.org,person=self.guard,shift=self.worked_post,
                kind=kind,occurred_at=when,client_event_id=uuid.uuid4())
        return payroll_rows(self.org, self.period_start, self.period_end)

    def test_the_code_resolves_post_then_site_then_contract_and_says_which_set_it(self):
        from .services import effective_pay_code
        post=effective_pay_code(Shift.objects.get(pk=self.post.pk))
        self.assertEqual(post["pay_code"],"")
        self.assertEqual(post["pay_code_source"],"")
        self.account.default_pay_code=self.patrol; self.account.save(update_fields=["default_pay_code"])
        resolved=effective_pay_code(Shift.objects.select_related("site__client").get(pk=self.post.pk))
        self.assertEqual((resolved["pay_code"],resolved["pay_code_source"]),("SEC-PATROL","contract"))
        self.site.default_pay_code=self.courier; self.site.save(update_fields=["default_pay_code"])
        resolved=effective_pay_code(Shift.objects.select_related("site__client").get(pk=self.post.pk))
        self.assertEqual((resolved["pay_code"],resolved["pay_code_source"]),("SEC-COURIER","site"))
        self.post.pay_code=self.patrol; self.post.save(update_fields=["pay_code"])
        resolved=effective_pay_code(Shift.objects.select_related("site__client").get(pk=self.post.pk))
        self.assertEqual((resolved["pay_code"],resolved["pay_code_source"]),("SEC-PATROL","post"))
        self.assertEqual(resolved["cost_centre"],"4102")

    def test_the_export_carries_the_code_category_and_cost_centre_as_columns(self):
        from .services import PAYROLL_EXPORT_FIELDS, payroll_csv
        self.worked_post.pay_code=self.patrol; self.worked_post.save(update_fields=["pay_code"])
        for field in ("pay_code","pay_code_name","cost_centre","pay_code_source","pay_category"):
            self.assertIn(field,PAYROLL_EXPORT_FIELDS)
        self.worked()
        text=payroll_csv(self.org,self.period_start,self.period_end)
        header=text.splitlines()[0].split(",")
        self.assertLess(header.index("pay_code"),header.index("client"))     # the key column leads
        row=text.splitlines()[1].split(",")
        self.assertEqual(row[header.index("pay_code")],"SEC-PATROL")
        self.assertEqual(row[header.index("pay_category")],"worked")
        self.assertEqual(row[header.index("cost_centre")],"4102")
        self.assertEqual(row[header.index("pay_code_source")],"post")

    def test_totals_group_by_pay_code_and_still_show_a_code_nobody_priced(self):
        from .services import payroll_totals
        self.worked_post.pay_code=self.courier; self.worked_post.save(update_fields=["pay_code"])
        rows=self.worked()
        totals=payroll_totals(rows,by="pay_code")
        self.assertEqual([item["key"] for item in totals],["SEC-COURIER"])
        self.assertEqual(totals[0]["total_hours"],Decimal("10.00"))
        self.assertEqual(totals[0]["employee_count"],1)
        self.courier.cost_centre=""                                        # no account attached
        self.courier.save(update_fields=["cost_centre"])
        self.assertEqual(payroll_totals(rows,by="pay_code")[0]["cost_centre"],"")

    def test_a_period_with_nothing_under_a_code_names_the_gap_instead_of_hiding_it(self):
        from .services import payroll_totals
        rows=self.worked()
        for row in rows:
            row["pay_code"]=""
        self.assertEqual(payroll_totals(rows,by="pay_code")[0]["key"],"(no pay_code)")

    def test_approved_leave_with_no_punch_behind_it_reaches_the_export_as_its_own_category(self):
        from datetime import timedelta
        from .services import ensure_pay_categories, payroll_rows
        # The whole point of PAY-3: a clerk reading only worked segments sees granted leave as unpaid
        # time, and nothing in the file says the absence was approved.
        #
        # The absence is given to its own officer on purpose. The earlier version of this fixture left
        # the leave on the same guard whose punches already cover that span, which made its name —
        # "no punch behind it" — false on its face: under the displaced-hours rule there was nothing to
        # pay, so the test was asserting a row that could not have failed.
        ensure_pay_categories(self.org)
        absent = Person.objects.create(organization=self.org, first_name="Otto", last_name="Gone",
                                       status=Person.Status.ACTIVE, hourly_rate=Decimal("18.00"))
        off_post = Shift.objects.create(organization=self.org, site=self.site, officer=absent,
                                        starts_at=self.start, ends_at=self.start + timedelta(hours=8),
                                        status=Shift.Status.PUBLISHED, post_name="Granted over")
        TimeOffRequest.objects.create(organization=self.org, person=self.guard, starts_at=self.start,
            ends_at=self.start+timedelta(hours=8),status=TimeOffRequest.Status.APPROVED)
        TimeOffRequest.objects.create(organization=self.org, person=absent, starts_at=self.start,
            ends_at=self.start + timedelta(hours=8), status=TimeOffRequest.Status.APPROVED)
        rows = self.worked()          # Rosa's punches, so her case really is "granted and worked"
        categories = sorted({row["pay_category"] for row in rows})
        self.assertEqual(categories, ["leave", "worked"])
        leave = next(row for row in rows if row["pay_category"] == "leave"
                     and row["employee_id"] == str(absent.pk))
        self.assertEqual(leave["raw_hours"], Decimal("8.00"))
        self.assertEqual(leave["total_hours"], Decimal("8.00"))
        self.assertEqual(leave["regular_hours"], Decimal("8.00"))
        self.assertEqual(leave["overtime_hours"], Decimal("0.00"),
                         "an absence is not hours worked, so it cannot push a week over the threshold")
        self.assertEqual(leave["estimated_pay"], Decimal("160.00"),
                         "8 displaced hours at the post's resolved rate — the site and contract rate, "
                         "not the officer's own, and not the calendar span")
        self.assertEqual(leave["estimated_bill"], "",
                         "a granted absence is the firm's cost; billing it to the client bills for nobody")
        self.assertIn("8.00 displaced hours across 1 post", leave["exception"])
        self.assertIn("not a payable figure", leave["exception"],
                      "the span it clipped stays readable, and stays not the number being paid")
        # The one that WAS worked keeps its row as evidence without claiming the hours a second time.
        worked_leave = next(row for row in rows if row["pay_category"] == "leave"
                            and row["employee_id"] == str(self.guard.pk))
        self.assertEqual(worked_leave["total_hours"], Decimal("0.00"))
        self.assertEqual(worked_leave["estimated_pay"], Decimal("0.00"),
                         "the worked rows already pay those hours; this row is evidence, not a duplicate")
        self.assertIn("was worked and is on the worked rows", worked_leave["exception"])
        self.assertEqual(off_post.status, Shift.Status.PUBLISHED)

    def test_leave_is_clipped_to_the_period_being_exported_rather_than_reported_whole(self):
        from datetime import timedelta
        from .services import payroll_rows
        TimeOffRequest.objects.create(organization=self.org,person=self.guard,
            starts_at=self.start-timedelta(days=2),ends_at=self.end+timedelta(days=2),
            status=TimeOffRequest.Status.APPROVED)
        rows=payroll_rows(self.org,self.start,self.end)
        leave=next(row for row in rows if row["pay_category"]=="leave")
        self.assertEqual(leave["raw_hours"],Decimal("10.00"))              # the window, not the request

    def test_declined_or_pending_leave_is_not_reported_as_time_off(self):
        from datetime import timedelta
        TimeOffRequest.objects.create(organization=self.org,person=self.guard,starts_at=self.start,
            ends_at=self.start+timedelta(hours=8),status=TimeOffRequest.Status.DECLINED)
        TimeOffRequest.objects.create(organization=self.org,person=self.guard,starts_at=self.start,
            ends_at=self.start+timedelta(hours=4),status=TimeOffRequest.Status.REQUESTED)
        self.assertEqual([row for row in self.worked() if row["pay_category"]=="leave"],[])

    def test_a_pay_code_in_use_is_closed_rather_than_deleted(self):
        self.post.pay_code=self.patrol; self.post.save(update_fields=["pay_code"])
        self.client.force_login(self.owner)
        self.client.post(reverse("pay_code_remove",args=[self.patrol.pk]))
        self.patrol.refresh_from_db()
        self.assertFalse(self.patrol.active)
        self.assertEqual(Shift.objects.get(pk=self.post.pk).pay_code_id,self.patrol.pk)   # history intact
        self.client.post(reverse("pay_code_remove",args=[self.courier.pk]))
        self.assertFalse(PayCode.objects.filter(pk=self.courier.pk).exists())             # unused goes away

    def test_another_tenants_code_is_not_a_choice_the_form_offers(self):
        self.client.force_login(self.owner)
        page=self.client.get(reverse("shift_create"))
        self.assertNotContains(page,"NOPE")
        response=self.client.post(reverse("shift_create"),{"site":self.site.pk,"officer":"","starts_at":self.start.strftime("%Y-%m-%dT%H:%M"),
            "ends_at":self.end.strftime("%Y-%m-%dT%H:%M"),"status":Shift.Status.DRAFT,"post_name":"Foreign code",
            "required_credentials":[],"pay_code":self.foreign.pk})
        self.assertEqual(response.status_code,200)
        self.assertFalse(Shift.objects.filter(post_name="Foreign code").exists())

    def test_the_schedule_shows_the_code_and_the_level_that_set_it(self):
        self.account.default_pay_code=self.patrol; self.account.save(update_fields=["default_pay_code"])
        self.client.force_login(self.owner)
        page=self.client.get(reverse("schedule")+"?layout=list")
        self.assertContains(page,"SEC-PATROL")
        self.assertContains(page,"code · contract")

    def test_the_payroll_page_groups_the_generated_period_by_code(self):
        from django.utils import timezone
        self.worked_post.pay_code=self.patrol; self.worked_post.save(update_fields=["pay_code"])
        self.worked()
        self.client.force_login(self.payroll_user)
        # The form takes a wall-clock time in the company's zone, so the aware UTC instants have to
        # be converted before being written out — posting UTC here would quietly shift the period
        # five hours sideways and generate an empty run.
        self.client.post(reverse("payroll"),{"period_start":timezone.localtime(self.period_start).strftime("%Y-%m-%dT%H:%M"),
            "period_end":timezone.localtime(self.period_end).strftime("%Y-%m-%dT%H:%M")})
        page=self.client.get(reverse("payroll"))
        self.assertContains(page,"By pay code")
        self.assertContains(page,"SEC-PATROL")
        self.assertContains(page,"Patrol post")
        self.assertContains(page,"4102")
        codes={item["key"] for item in page.context["by_pay_code"]}
        self.assertEqual(codes,{"SEC-PATROL"})
        self.assertIn("worked",{item["category"] for item in page.context["by_category"]})

    def test_the_code_list_reports_what_is_standing_on_each_one(self):
        self.post.pay_code=self.patrol; self.post.save(update_fields=["pay_code"])
        self.site.default_pay_code=self.patrol; self.site.save(update_fields=["default_pay_code"])
        self.client.force_login(self.owner)
        page=self.client.get(reverse("pay_codes"))
        self.assertContains(page,"SEC-PATROL")
        self.assertContains(page,"1 post")
        self.assertContains(page,"1 site")


class ShiftExchangeTest(TestCase):
    """A two-way trade, decided once.

    Every peer that ships an exchange makes the pair a single approval, because approving one leg
    releases an officer from a post while nobody has taken the other — a coverage hole the schedule
    would still show as filled. These tests hold that, plus the hours-before/after the manager is
    owed, and the rule that a manager who is one of the two officers cannot decide it.
    """

    def setUp(self):
        from datetime import datetime, time, timedelta
        from django.utils import timezone
        User=get_user_model()
        self.owner=User.objects.create_user(username="ex-owner@example.com",password="correct horse battery staple")
        self.rosa_user=User.objects.create_user(username="ex-rosa@example.com",password="correct horse battery staple")
        self.sam_user=User.objects.create_user(username="ex-sam@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Exchange LLC",display_name="Exchange",slug="exchange")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        for user in (self.rosa_user,self.sam_user):
            Membership.objects.create(user=user,organization=self.org,role=Membership.Role.OFFICER)
        self.rosa=Person.objects.create(organization=self.org,user=self.rosa_user,first_name="Rosa",last_name="Vega",
            status=Person.Status.ACTIVE,is_commissioned_officer=True)
        self.sam=Person.objects.create(organization=self.org,user=self.sam_user,first_name="Sam",last_name="Okafor",
            status=Person.Status.ACTIVE,is_commissioned_officer=True)
        self.account=Client.objects.create(organization=self.org,name="Ridgeline Retail")
        self.gate=Site.objects.create(organization=self.org,client=self.account,name="Gate 1",address="1 Gate Rd")
        self.dock=Site.objects.create(organization=self.org,client=self.account,name="Dock",address="2 Dock Rd")
        policy,_=TimePolicy.objects.get_or_create(organization=self.org,defaults={"timezone":self.org.timezone})
        today=timezone.localdate()
        # Both posts anchored inside *one* workweek, deliberately. "For a swap, both employees keep
        # their hours" is a per-week statement, so the old `now+2d` / `now+3d` fixture only held when
        # the run day happened to leave both posts in the same week — it passed on a Thursday night
        # and failed on a Friday, which is a clock artefact, not a product one.
        week_start=today-timedelta(days=(today.weekday()-policy.workweek_start)%7)+timedelta(days=7)
        def span(day):
            starts=timezone.make_aware(datetime.combine(week_start+timedelta(days=day),time(8,0)))
            return starts,starts+timedelta(hours=12)
        rosa_start,rosa_end=span(0)
        sam_start,sam_end=span(1)
        self.rosa_post=Shift.objects.create(organization=self.org,site=self.gate,starts_at=rosa_start,
            ends_at=rosa_end,status=Shift.Status.PUBLISHED,officer=self.rosa,post_name="Gate twelves")
        self.sam_post=Shift.objects.create(organization=self.org,site=self.dock,starts_at=sam_start,
            ends_at=sam_end,status=Shift.Status.PUBLISHED,officer=self.sam,post_name="Dock twelves")

    def propose(self,partner=None):
        self.client.force_login(self.rosa_user)
        return self.client.post(reverse("offer_post",args=[self.rosa_post.pk]),
            {"partner":(partner or self.sam).pk,"note":"wedding","mode":"trade"})

    def accept(self,exchange,chosen=None):
        self.client.force_login(self.sam_user)
        return self.client.post(reverse("exchange_respond",args=[exchange.pk]),
            {"action":ShiftExchange.Status.AGREED,"partner_shift":(chosen or self.sam_post).pk,"note":""})

    def decide(self,exchange,action=ShiftExchange.Status.APPROVED,note=""):
        self.client.force_login(self.owner)
        return self.client.post(reverse("exchange_decide",args=[exchange.pk]),{"action":action,"note":note})

    def test_a_trade_moves_both_posts_on_one_approval(self):
        self.propose()
        exchange=ShiftExchange.objects.get(initiator_shift=self.rosa_post)
        self.assertEqual(exchange.status,ShiftExchange.Status.PROPOSED)
        self.assertIsNone(exchange.partner_shift_id)          # the partner names their own post, not the proposer
        self.rosa_post.refresh_from_db(); self.sam_post.refresh_from_db()
        self.assertEqual(self.rosa_post.officer_id,self.rosa.pk)
        self.assertEqual(self.sam_post.officer_id,self.sam.pk)
        self.accept(exchange)
        exchange.refresh_from_db()
        self.assertEqual(exchange.status,ShiftExchange.Status.AGREED)
        self.assertEqual(exchange.partner_shift_id,self.sam_post.pk)
        self.rosa_post.refresh_from_db()
        self.assertEqual(self.rosa_post.officer_id,self.rosa.pk)   # still nothing moved pre-approval
        queue=self.client.get(reverse("swaps"))                     # an officer has no approval surface
        self.assertEqual(queue.status_code,403)
        self.client.force_login(self.owner)
        queue=self.client.get(reverse("swaps"))
        self.assertContains(queue,"Both agreed")
        self.decide(exchange)
        exchange.refresh_from_db(); self.rosa_post.refresh_from_db(); self.sam_post.refresh_from_db()
        self.assertEqual(exchange.status,ShiftExchange.Status.APPROVED)
        self.assertEqual(self.rosa_post.officer_id,self.sam.pk)     # both moved
        self.assertEqual(self.sam_post.officer_id,self.rosa.pk)
        self.assertTrue(AuditEvent.objects.filter(action="shift.exchange_approved").exists())

    def test_refusing_the_pair_moves_neither_post(self):
        self.propose(); exchange=ShiftExchange.objects.get(initiator_shift=self.rosa_post)
        self.accept(exchange)
        self.decide(exchange,action=ShiftExchange.Status.REFUSED,note="gate needs the regular")
        exchange.refresh_from_db(); self.rosa_post.refresh_from_db(); self.sam_post.refresh_from_db()
        self.assertEqual(exchange.status,ShiftExchange.Status.REFUSED)
        self.assertEqual(self.rosa_post.officer_id,self.rosa.pk)
        self.assertEqual(self.sam_post.officer_id,self.sam.pk)

    def test_a_trade_is_refused_when_either_direction_fails_qualification(self):
        from datetime import timedelta
        from django.utils import timezone
        armed=CredentialType.objects.create(organization=self.org,name="Firearms",code="firearms",
            applies_to=["commissioned"],blocks_scheduling=True)
        self.propose(); exchange=ShiftExchange.objects.get(initiator_shift=self.rosa_post)
        self.accept(exchange)
        # The requirement appears *after* both officers agreed — which is the whole reason the pair
        # is re-checked at approval rather than trusted from the offer.
        self.rosa_post.required_credentials.add(armed)
        self.decide(exchange)
        exchange.refresh_from_db(); self.rosa_post.refresh_from_db(); self.sam_post.refresh_from_db()
        self.assertEqual(exchange.status,ShiftExchange.Status.AGREED)   # left open, not silently approved
        self.assertEqual(self.rosa_post.officer_id,self.rosa.pk)
        self.assertEqual(self.sam_post.officer_id,self.sam.pk)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("swaps")),"Firearms")   # the failing direction is named
        # The reverse direction is the one that fails, so fixing it and approving moves both.
        Credential.objects.create(organization=self.org,person=self.sam,credential_type=armed,
            status=Credential.Status.ACTIVE,number="F-1",expires_on=timezone.localdate()+timedelta(days=90))
        self.decide(exchange)
        self.rosa_post.refresh_from_db(); self.sam_post.refresh_from_db()
        self.assertEqual(self.rosa_post.officer_id,self.sam.pk)
        self.assertEqual(self.sam_post.officer_id,self.rosa.pk)

    def test_a_like_for_like_trade_leaves_both_officers_hours_unchanged(self):
        from .services import assignment_impact
        impact=assignment_impact(self.org,[(self.rosa_post,self.rosa,self.sam),
                                           (self.sam_post,self.sam,self.rosa)])
        self.assertTrue(impact)
        for row in impact:
            self.assertEqual(row["before"],row["after"],row["person"].full_name)
            self.assertFalse(row["over_after"])

    def test_the_partner_can_only_put_in_a_post_they_are_actually_standing(self):
        self.propose(); exchange=ShiftExchange.objects.get(initiator_shift=self.rosa_post)
        self.client.force_login(self.sam_user)
        # Naming the proposer's own post would make the trade a no-op that still "moved" two rows.
        response=self.client.post(reverse("exchange_respond",args=[exchange.pk]),
            {"action":ShiftExchange.Status.AGREED,"partner_shift":self.rosa_post.pk,"note":""})
        self.assertRedirects(response,reverse("my_shifts"))
        exchange.refresh_from_db()
        self.assertEqual(exchange.status,ShiftExchange.Status.PROPOSED)
        self.assertIsNone(exchange.partner_shift_id)
        self.rosa_post.refresh_from_db()
        self.assertEqual(self.rosa_post.officer_id,self.rosa.pk)

    def test_a_manager_who_is_one_of_the_two_cannot_approve_their_own_trade(self):
        Membership.objects.filter(user=self.sam_user,organization=self.org).update(role=Membership.Role.SUPERVISOR)
        self.propose(); exchange=ShiftExchange.objects.get(initiator_shift=self.rosa_post)
        self.accept(exchange)
        self.client.force_login(self.sam_user)
        self.client.post(reverse("exchange_decide",args=[exchange.pk]),
            {"action":ShiftExchange.Status.APPROVED,"note":""})
        exchange.refresh_from_db(); self.rosa_post.refresh_from_db(); self.sam_post.refresh_from_db()
        self.assertEqual(exchange.status,ShiftExchange.Status.AGREED)
        self.assertEqual(self.rosa_post.officer_id,self.rosa.pk)     # the one independent consent is not his

    def test_a_manager_can_raise_a_trade_but_the_other_officer_still_has_to_answer(self):
        Membership.objects.filter(user=self.owner,organization=self.org).delete()
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.SCHEDULER)
        self.client.force_login(self.owner)
        response=self.client.post(reverse("offer_post",args=[self.rosa_post.pk]),
            {"partner":self.sam.pk,"note":"client moved the rota","mode":"trade"})
        self.assertEqual(response.status_code,302)
        exchange=ShiftExchange.objects.get(initiator_shift=self.rosa_post)
        self.assertEqual(exchange.raised_by,ShiftExchange.RaisedBy.MANAGER)
        self.assertEqual(exchange.created_by_id,self.owner.pk)
        self.assertEqual(exchange.status,ShiftExchange.Status.PROPOSED)   # not agreed, not approved
        self.assertTrue(Notification.objects.filter(recipient=self.rosa_user,event_type="shift.exchange_proposed").exists())
        self.rosa_post.refresh_from_db()
        self.assertEqual(self.rosa_post.officer_id,self.rosa.pk)

    def test_declining_a_trade_moves_nothing_and_tells_the_proposer(self):
        self.propose(); exchange=ShiftExchange.objects.get(initiator_shift=self.rosa_post)
        self.client.force_login(self.sam_user)
        self.client.post(reverse("exchange_respond",args=[exchange.pk]),{"action":ShiftExchange.Status.DECLINED})
        exchange.refresh_from_db(); self.rosa_post.refresh_from_db()
        self.assertEqual(exchange.status,ShiftExchange.Status.DECLINED)
        self.assertEqual(self.rosa_post.officer_id,self.rosa.pk)
        self.assertTrue(Notification.objects.filter(recipient=self.rosa_user,event_type="shift.exchange_declined").exists())

    def test_one_request_is_in_flight_per_post_and_the_proposer_can_withdraw_it(self):
        self.propose(); exchange=ShiftExchange.objects.get(initiator_shift=self.rosa_post)
        self.assertEqual(self.propose().status_code,302)     # a duplicate is recognised, not created
        self.assertEqual(ShiftExchange.objects.count(),1)
        self.client.force_login(self.sam_user)
        self.client.post(reverse("exchange_respond",args=[exchange.pk]),{"action":ShiftExchange.Status.DECLINED})
        self.assertEqual(self.propose().status_code,302)     # once closed, asking again works
        self.assertEqual(ShiftExchange.objects.count(),2)
        newest=ShiftExchange.objects.order_by("-created_at").first()
        self.client.force_login(self.rosa_user)
        self.client.post(reverse("exchange_withdraw",args=[newest.pk]))
        newest.refresh_from_db()
        self.assertEqual(newest.status,ShiftExchange.Status.WITHDRAWN)

    def test_an_unanswered_trade_expires_once_a_post_in_it_has_started(self):
        from datetime import timedelta
        from django.utils import timezone
        from .services import expire_stale_moves
        self.propose(); exchange=ShiftExchange.objects.get(initiator_shift=self.rosa_post)
        self.accept(exchange)
        self.assertEqual(expire_stale_moves(self.org),0)          # both posts are still ahead
        past=timezone.now()-timedelta(hours=1)
        Shift.objects.filter(pk=self.rosa_post.pk).update(starts_at=past,ends_at=past+timedelta(hours=12))
        self.assertEqual(expire_stale_moves(self.org),1)
        exchange.refresh_from_db(); self.rosa_post.refresh_from_db(); self.sam_post.refresh_from_db()
        self.assertEqual(exchange.status,ShiftExchange.Status.EXPIRED)
        self.assertEqual(self.rosa_post.officer_id,self.rosa.pk)   # nothing moved at expiry
        self.assertEqual(self.sam_post.officer_id,self.sam.pk)
        self.assertTrue(Notification.objects.filter(recipient=self.rosa_user,event_type="shift.exchange_expired").exists())

    def test_the_queue_lists_a_trade_with_both_directions_and_its_hours(self):
        self.propose(); exchange=ShiftExchange.objects.get(initiator_shift=self.rosa_post)
        self.accept(exchange)
        self.client.force_login(self.owner)
        page=self.client.get(reverse("swaps"))
        self.assertContains(page,"Trades waiting on you")
        self.assertContains(page,"Gate twelves")
        self.assertContains(page,"Dock twelves")
        self.assertContains(page,"A trade should leave both figures unchanged")
        self.assertEqual(len(page.context["exchanges"]),1)

    def test_the_trade_appears_on_both_officers_own_pages(self):
        self.client.force_login(self.rosa_user)
        self.assertContains(self.client.get(reverse("my_shifts")),"Move this post")
        self.propose()
        page=self.client.get(reverse("my_shifts"))
        self.assertContains(page,"Trade with Sam Okafor")
        self.assertNotContains(page,"Move this post")            # one request in flight per post
        self.client.force_login(self.sam_user)
        incoming=self.client.get(reverse("my_shifts"))
        self.assertContains(incoming,"Trades offered to you")
        self.assertContains(incoming,"You qualify for their post")
        self.assertContains(incoming,"Which of your posts do you trade")


class OfflineClockLaunchTest(TestCase):
    """CLK-5: a guard who closed the tab in a stairwell must be able to reopen the clock.

    The queue side was already sound — punches are sealed in IndexedDB under a non-exportable key, so
    they survive a cold start. What was missing is the page: the old worker cached only the shell, so
    reopening /clock/ without signal gave a browser error and the encrypted queue had nothing to run
    against. These assertions cover what is checkable without a browser; the real offline reopen still
    needs a device and stays an open production gate (see roadmap PLT-4).
    """

    def setUp(self):
        User=get_user_model()
        self.owner=User.objects.create_user(username="offline-owner@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Offline LLC",display_name="Offline",slug="offline")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        # Without a personnel record /clock/ renders the "link required" branch, which has no clock
        # card and so nothing to assert against.
        Person.objects.create(organization=self.org,user=self.owner,first_name="Ops",last_name="Officer",
            status=Person.Status.ACTIVE,is_unarmed_officer=True)

    def worker(self):
        self.client.force_login(self.owner)
        return self.client.get(reverse("service_worker")).content.decode()

    def test_the_service_worker_names_the_clock_as_a_cached_document(self):
        body=self.worker()
        self.assertIn('PAGES=["/clock/"]',body)
        # Bumped cache names are what flush the old shell-only cache. Without them an install that
        # predates this change keeps serving the previous worker, and the feature silently never
        # arrives on the devices that most need it.
        self.assertIn("tscm-shell-v2",body)
        self.assertIn("tscm-docs-v2",body)
        self.assertIn("caches.delete(name)",body)
        self.assertIn("clock-served-from-cache",body)

    def test_the_clock_page_is_storable_rather_than_no_store(self):
        # The quiet failure this guards: a never_cache on the view would leave every other line of
        # the feature looking correct while nothing was ever written to the cache.
        self.client.force_login(self.owner)
        self.assertNotIn("no-store",self.client.get(reverse("clock")).get("Cache-Control",""))

    def test_the_clock_carries_a_staleness_banner_the_client_can_reveal(self):
        self.client.force_login(self.owner)
        page=self.client.get(reverse("clock"))
        self.assertContains(page,"data-clock-offline")
        self.assertContains(page,"Working without signal")

    def test_the_client_admits_staleness_and_refreshes_itself_on_reconnection(self):
        from pathlib import Path
        script=Path("static/js/app.js").read_text(encoding="utf-8")
        self.assertIn("data-clock-offline",script)
        self.assertIn("clock-served-from-cache",script)
        self.assertIn("window.location.reload()",script)

    def test_a_worker_with_nothing_cached_says_so_rather_than_showing_a_browser_error(self):
        body=self.worker()
        self.assertIn("No saved clock page",body)
        self.assertIn("not lost",body)


class RecordSegregationTest(TestCase):
    """REC-5: the secrecy ladder is honoured by every path that reads a filed record.

    `audience` says who a record is *issued to*, not who may *open* one, so a workers' compensation
    claim and a payroll receipt both land in one person's file with the same readers — which is the
    opposite of what RB §HCRM record catalog family 4 asks for ("isolated by stricter permissions
    rather than exposed in the general personnel file"). The regression this guards is silent by
    construction: a listing that stops filtering still renders, and a download route that stops
    checking still serves. So each read path gets its own assertion, and the listing filter and the
    row-level check are asserted to *agree*, not merely to exist.
    """

    def setUp(self):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        User=get_user_model()
        self.owner=User.objects.create_user(username="seg-owner@example.com",password="correct horse battery staple")
        self.hr=User.objects.create_user(username="seg-hr@example.com",password="correct horse battery staple")
        self.auditor=User.objects.create_user(username="seg-auditor@example.com",password="correct horse battery staple")
        self.scheduler=User.objects.create_user(username="seg-scheduler@example.com",password="correct horse battery staple")
        self.worker=User.objects.create_user(username="seg-guard@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Segregation LLC",display_name="Segregation",slug="segregation-llc")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.hr,organization=self.org,role=Membership.Role.HR)
        Membership.objects.create(user=self.auditor,organization=self.org,role=Membership.Role.AUDITOR)
        Membership.objects.create(user=self.scheduler,organization=self.org,role=Membership.Role.SCHEDULER)
        Membership.objects.create(user=self.worker,organization=self.org,role=Membership.Role.OFFICER)
        self.person=Person.objects.create(organization=self.org,user=self.worker,first_name="Ana",last_name="Delgado",status=Person.Status.ACTIVE)
        # The claim and the payroll receipt are the same shape to `audience`: both attached to one
        # person. Only the rung separates them.
        self.receipt=DocumentType.objects.create(organization=self.org,name="Direct deposit authorization",code="deposit")
        self.claim=DocumentType.objects.create(organization=self.org,name="Workers compensation claim",code="claim",sensitivity=DocumentType.Sensitivity.RESTRICTED)
        self.discipline=DocumentType.objects.create(organization=self.org,name="Investigation file",code="investigation",sensitivity=DocumentType.Sensitivity.SEALED)
        self.handbook=DocumentType.objects.create(organization=self.org,name="Employee handbook",code="handbook",audience=DocumentType.Audience.WORKFORCE,acknowledgment_required=True)
        self.licence=DocumentType.objects.create(organization=self.org,name="Class B licence",code="class-b",audience=DocumentType.Audience.MANAGEMENT)
        self.files=SimpleUploadedFile
        media=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        override=override_settings(MEDIA_ROOT=media.name); override.enable()
        self.addCleanup(override.disable); self.addCleanup(media.cleanup)
        self.file(self.receipt,self.person,"deposit.pdf")
        self.file(self.claim,self.person,"claim.pdf")
        self.file(self.discipline,self.person,"investigation.pdf")
        self.file(self.handbook,None,"handbook.pdf")
        self.file(self.licence,None,"licence.pdf")

    def file(self,document_type,person,name):
        return PersonDocument.objects.create(organization=self.org,person=person,document_type=document_type,
            file=self.files(name,b"%PDF-1.4\nbody",content_type="application/pdf"),original_name=name,
            content_type="application/pdf",size=13,sha256="0"*64,scan_status=PersonDocument.ScanStatus.CLEAN)

    # -- the two definitions of the rule --

    def test_the_listing_filter_and_the_row_check_never_disagree(self):
        """A register that shows a row the download route refuses 404s the user on their own file.

        The comparison is set-equality over the whole corpus for every role, in both directions: a
        filter that lost its exclusion widens `listed`, a row check that lost its subject branch
        narrows `opened`, and either one fails here rather than in a browser.
        """
        from .models import record_readable, record_visibility_filter
        corpus=list(PersonDocument.objects.select_related("document_type"))
        for role in (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR,
                    Membership.Role.AUDITOR, Membership.Role.SCHEDULER, Membership.Role.OFFICER):
            listed={row.pk for row in PersonDocument.objects.filter(record_visibility_filter(role,self.person.pk))}
            opened={row.pk for row in corpus if record_readable(row,role,self.person.pk)}
            self.assertEqual(listed,opened,f"{role} sees {len(listed)} rows in a list and {len(opened)} behind a link")

    def test_the_standard_rung_is_the_reader_set_the_views_already_gated_on(self):
        """The ladder widened nothing: rung one is exactly the old single gate, spelled out."""
        from .models import DOCUMENT_TYPE_READERS
        from . import views
        self.assertEqual(set(DOCUMENT_TYPE_READERS[DocumentType.Sensitivity.STANDARD.value]),set(views.RECORD_READERS))

    def test_a_role_that_is_not_on_the_ladder_opens_nothing(self):
        from .models import staff_sensitivities
        self.assertEqual(staff_sensitivities("payroll"),())
        self.assertEqual(staff_sensitivities(Membership.Role.OFFICER),())

    # -- the download route --

    def test_an_outside_auditor_cannot_download_the_claim_or_the_investigation_file(self):
        self.client.force_login(self.auditor)
        self.assertEqual(self.client.get(reverse("document_download",args=[PersonDocument.objects.get(original_name="deposit.pdf").pk])).status_code,200)
        self.assertEqual(self.client.get(reverse("document_download",args=[PersonDocument.objects.get(original_name="claim.pdf").pk])).status_code,404)
        self.assertEqual(self.client.get(reverse("document_download",args=[PersonDocument.objects.get(original_name="investigation.pdf").pk])).status_code,404)

    def test_the_owner_and_hr_still_open_every_rung_of_someone_elses_file(self):
        for user in (self.owner,self.hr):
            self.client.force_login(user)
            for name in ("claim.pdf","investigation.pdf"):
                self.assertEqual(self.client.get(reverse("document_download",args=[PersonDocument.objects.get(original_name=name).pk])).status_code,200,f"{user.username} should open {name}")

    def test_a_refused_download_leaves_no_audit_row_behind(self):
        # The audit event is written after the decision, so a 404 must not log a read that never
        # happened: an auditor's log of "downloaded investigation.pdf" would be evidence of a
        # disclosure the system actually prevented.
        self.client.force_login(self.auditor)
        claim=PersonDocument.objects.get(original_name="claim.pdf")
        self.client.get(reverse("document_download",args=[claim.pk]))
        self.assertFalse(AuditEvent.objects.filter(action="document.downloaded",target_id=str(claim.pk)).exists())

    # -- the worker's own file --

    def test_the_officer_reads_their_own_claim_and_never_the_file_about_them(self):
        self.client.force_login(self.worker)
        page=self.client.get(reverse("my_documents"))
        self.assertContains(page,"Workers compensation claim")
        self.assertNotContains(page,"Investigation file")
        self.assertEqual(self.client.get(reverse("document_download",args=[PersonDocument.objects.get(original_name="claim.pdf").pk])).status_code,200)
        self.assertEqual(self.client.get(reverse("document_download",args=[PersonDocument.objects.get(original_name="investigation.pdf").pk])).status_code,404)

    def test_a_sealed_record_stays_hidden_from_its_subject_from_an_hr_sign_in(self):
        """Being the subject outranks being staff: the reader list is for other people's files."""
        self.client.force_login(self.hr)
        subject=Person.objects.create(organization=self.org,user=self.hr,first_name="Rosa",last_name="Vega",status=Person.Status.ACTIVE)
        mine=self.file(self.discipline,subject,"my-investigation.pdf")
        others=self.file(self.discipline,self.person,"their-investigation.pdf")
        self.assertEqual(self.client.get(reverse("document_download",args=[mine.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse("document_download",args=[others.pk])).status_code,200)

    def test_guessing_a_sealed_records_uuid_does_not_allow_a_signature_on_it(self):
        from .models import DocumentAcknowledgment
        # Acknowledging is a read: the form renders the title and binds a signature to the file.
        self.client.force_login(self.worker)
        sealed=PersonDocument.objects.get(original_name="investigation.pdf")
        self.assertEqual(self.client.get(reverse("document_acknowledge",args=[sealed.pk])).status_code,404)
        self.assertFalse(DocumentAcknowledgment.objects.filter(document=sealed).exists())

    # -- the register, the tab, and the roster screen --

    def test_the_records_register_hides_segregated_rows_from_an_auditor(self):
        self.client.force_login(self.auditor)
        page=self.client.get(reverse("documents"))
        self.assertContains(page,"Direct deposit authorization")
        self.assertNotContains(page,"Workers compensation claim")
        self.assertNotContains(page,"Investigation file")

    def test_the_workers_own_documents_tab_hides_the_file_about_them(self):
        # An auditor cannot reach a person profile at all (`_profile_person` requires MANAGERS), so
        # the subject case is the one this tab can actually leak: opening your own file must not
        # return the investigation row, even though the tab is gated by `is_self`.
        self.client.force_login(self.worker)
        page=self.client.get(reverse("person_detail",args=[self.person.pk])+"?tab=documents")
        self.assertContains(page,"Workers compensation claim")
        self.assertNotContains(page,"Investigation file")

    def test_a_record_manager_opening_someone_elses_file_still_sees_every_rung(self):
        # The other direction: the same filter must not over-hide, or the personnel file a manager
        # is entitled to work would quietly lose its segregated rows.
        self.client.force_login(self.hr)
        page=self.client.get(reverse("person_detail",args=[self.person.pk])+"?tab=documents")
        self.assertContains(page,"Investigation file")
        self.assertContains(page,"Workers compensation claim")

    def test_the_acknowledgment_roster_screen_refuses_a_rung_the_reader_cannot_open(self):
        """The roster names who has *not* signed, which is its own disclosure."""
        sealed=PersonDocument.objects.get(original_name="investigation.pdf")
        handbook=PersonDocument.objects.get(original_name="handbook.pdf")
        self.client.force_login(self.auditor)
        self.assertEqual(self.client.get(reverse("document_acknowledgments",args=[sealed.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse("document_acknowledgments",args=[handbook.pk])).status_code,200)

    def test_a_scheduler_still_opens_no_record_route_at_all(self):
        # The pre-existing gate, re-pinned: the ladder sits inside it, it does not replace it.
        self.client.force_login(self.scheduler)
        self.assertEqual(self.client.get(reverse("documents")).status_code,403)
        self.assertEqual(self.client.get(reverse("document_download",args=[PersonDocument.objects.get(original_name="claim.pdf").pk])).status_code,404)

    # -- the company-issued cases the ladder must not disturb --

    def test_the_handbook_issued_to_every_worker_stays_readable_for_a_worker(self):
        self.client.force_login(self.worker)
        handbook=PersonDocument.objects.get(original_name="handbook.pdf")
        self.assertEqual(self.client.get(reverse("document_download",args=[handbook.pk])).status_code,200)
        self.assertContains(self.client.get(reverse("my_documents")),"Employee handbook")

    def test_a_management_company_record_is_still_officers_only(self):
        licence=PersonDocument.objects.get(original_name="licence.pdf")
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse("document_download",args=[licence.pk])).status_code,404)
        self.client.force_login(self.auditor)
        self.assertEqual(self.client.get(reverse("document_download",args=[licence.pk])).status_code,200)

    def test_a_record_type_predating_the_ladder_is_not_silently_hidden(self):
        # Every existing row gets `standard`, so the migration must not narrow anybody's existing
        # access; a default of `restricted` would hide the whole personnel file from an auditor.
        for document_type in (self.receipt,self.handbook,self.licence):
            self.assertEqual(document_type.sensitivity,DocumentType.Sensitivity.STANDARD)

    # -- the queue and the number computed from it --

    def test_the_compliance_queue_counts_only_the_records_the_reader_may_open(self):
        from .scope import for_membership
        from .services import compliance_attendance
        # Each reader gets their *own* subject id: passing the officer's person row to the auditor
        # would hand them the claim through the subject axis and make the denominator a fixture
        # artifact rather than the thing the queue actually shows.
        reader=lambda membership: (membership.role, membership.organization.people.filter(user=membership.user).values_list("id",flat=True).first())
        section=lambda membership: compliance_attendance(self.org,for_membership(membership),reader=reader(membership))["documents"]
        auditor=section(self.org.memberships.get(user=self.auditor))
        hr=section(self.org.memberships.get(user=self.hr))
        # Rung one is the receipt, the handbook, and the company licence; the claim and the
        # investigation file are only in HR's denominator.
        self.assertEqual(auditor["total"],3)
        self.assertEqual(hr["total"],5)
        self.assertNotEqual(auditor["total"],hr["total"],"a shared denominator would count what one reader cannot open")

    def test_the_queue_withholds_the_record_section_when_no_ladder_was_supplied(self):
        from .scope import for_membership
        from .services import compliance_attendance
        built=compliance_attendance(self.org,for_membership(self.org.memberships.get(user=self.hr)))
        self.assertEqual(built["documents"]["total"],0)

    def test_the_settings_screen_names_the_roles_each_rung_leaves(self):
        self.client.force_login(self.owner)
        page=self.client.get(reverse("settings_compliance"))
        self.assertContains(page,"Read-only auditor")
        self.assertContains(page,"not the officer the record is about")
        self.assertContains(page,"Restricted")


class ComplianceDutyTest(TestCase):
    """CMP-0: a duty that is not a credential can be entered, versioned, and measured honestly.

    The matrix had one subject — `CredentialType` — so the obligations the research pass actually
    recorded had nowhere to go: the certificate of liability the *licensee* holds under §1702.124,
    the posting duty of §1702.128 / 37 TAC §35.8, the owner training **DD §HCRM** names by title. The
    tests below are split by what the product can now tell the truth about, because a compliance
    screen that scores a duty it cannot check is worse than one that leaves it out.
    """

    def setUp(self):
        import tempfile
        from datetime import timedelta
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from django.utils import timezone
        User=get_user_model()
        self.today=timezone.localdate()
        self.owner=User.objects.create_user(username="duty-owner@example.com",password="correct horse battery staple")
        self.armorer=User.objects.create_user(username="duty-armed@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Duty LLC",display_name="Duty",slug="duty-llc")
        Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        Membership.objects.create(user=self.armorer,organization=self.org,role=Membership.Role.OFFICER)
        self.commissioned=Person.objects.create(organization=self.org,user=self.armorer,first_name="Ray",last_name="Ortiz",status=Person.Status.ACTIVE,is_commissioned_officer=True)
        self.unarmed=Person.objects.create(organization=self.org,first_name="Beth",last_name="Cole",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        self.certificate=DocumentType.objects.create(organization=self.org,name="Liability certificate",code="cgl",audience=DocumentType.Audience.MANAGEMENT)
        self.requirement=CredentialType.objects.create(organization=self.org,name="Guard registration",code="guard-reg",applies_to=["unarmed"],jurisdiction="Texas",authority_url="https://statutes.capitol.texas.gov/Docs/OC/htm/OC.1702.htm",authority_reference="§1702.302",interpretation="Every unarmed officer holds a current registration.",effective_from=self.today-timedelta(days=100),approved_by=self.owner,approved_at=timezone.now())
        self.duty=ComplianceRule.objects.create(organization=self.org,name="Commercial general liability coverage",code="cgl-hold",
            evidence=ComplianceRule.Evidence.DOCUMENT,applies_to_subject=ComplianceRule.Subject.ORGANIZATION,document_type=self.certificate,
            jurisdiction="Texas",authority_url="https://statutes.capitol.texas.gov/Docs/OC/htm/OC.1702.htm",
            authority_reference="Tex. Occ. Code §1702.124",interpretation="The licensee holds a current certificate at the required limits.",
            effective_from=self.today-timedelta(days=100),warning_days=45,approved_by=self.owner,approved_at=timezone.now())
        self.files=SimpleUploadedFile
        # Local imports live in setUp, so the helpers and the later tests reach these through self.
        self.User=User
        self.timedelta=timedelta
        self.timezone=timezone
        media=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        override=override_settings(MEDIA_ROOT=media.name); override.enable()
        self.addCleanup(override.disable); self.addCleanup(media.cleanup)
        self.client.force_login(self.owner)

    def file_certificate(self,expires_in=None,person=None):
        return PersonDocument.objects.create(organization=self.org,person=person,document_type=self.certificate,
            file=self.files("cgl.pdf",b"%PDF-1.4\ncert",content_type="application/pdf"),original_name="cgl.pdf",
            content_type="application/pdf",size=11,sha256="0"*64,scan_status=PersonDocument.ScanStatus.CLEAN,
            expires_on=(self.today+self.timedelta(days=expires_in)) if expires_in is not None else None)

    def duties(self,membership=None):
        from .scope import for_membership
        from .services import compliance_attendance
        membership=membership or self.org.memberships.get(user=self.owner)
        subject=self.org.people.filter(user=membership.user).values_list("id",flat=True).first()
        return compliance_attendance(self.org,for_membership(membership),
            reader=(membership.role,subject))["duties"]

    # -- entering a duty --

    def test_a_duty_is_entered_through_the_matrix_with_the_same_columns_as_a_requirement(self):
        page=self.client.get(reverse("settings_compliance"))
        self.assertContains(page,"Commercial general liability coverage")
        self.assertContains(page,"Tex. Occ. Code §1702.124")
        self.assertContains(page,"A filed record of the named type")
        self.assertContains(page,"Liability certificate")
        # One matrix, not two screens: the credential requirement and the duty are in the same table.
        self.assertContains(page,"Guard registration")
        self.assertContains(page,"Control matrix")
        self.assertNotContains(page,"Credential control matrix")

    def test_a_document_duty_cannot_be_saved_without_naming_its_evidence(self):
        # An approval is a claim that the rule is enforceable, so the row that makes it must name
        # the thing that proves it; otherwise the matrix would show a duty nothing can ever close.
        response=self.client.post(reverse("compliance_rule_create"),{"name":"Posted notice","code":"posting","evidence":ComplianceRule.Evidence.DOCUMENT,
            "applies_to_subject":ComplianceRule.Subject.ORGANIZATION,"document_type":"","required_hours":"","jurisdiction":"Texas",
            "authority_url":"https://www.dps.texas.gov/section/private-security","authority_reference":"§1702.128","interpretation":"Notice posted.",
            "effective_from":"","effective_until":"","warning_days":"60","reminder_days_before":"","active":"on"})
        self.assertEqual(response.status_code,200)
        self.assertFalse(ComplianceRule.objects.filter(code="posting").exists())

    def test_a_duty_of_each_evidence_kind_can_be_recorded_and_read_back(self):
        for kind in ComplianceRule.Evidence.values:
            response=self.client.post(reverse("compliance_rule_create"),{"name":f"Duty {kind}","code":f"duty-{kind}",
                "evidence":kind,"applies_to_subject":ComplianceRule.Subject.ORGANIZATION,
                "document_type":self.certificate.pk if kind==ComplianceRule.Evidence.DOCUMENT else "",
                "required_hours":"", "jurisdiction":"Texas","authority_url":"https://www.dps.texas.gov/section/private-security",
                "authority_reference":f"§1702 · {kind}","interpretation":"Approved reading.","effective_from":"","effective_until":"",
                "warning_days":"60","reminder_days_before":"90, 30","active":"on","approve":"yes"})
            self.assertRedirects(response,reverse("settings_compliance"))
            rule=ComplianceRule.objects.get(code=f"duty-{kind}")
            self.assertTrue(rule.is_approved)
            self.assertEqual(rule.reminder_levels,[90,60,30])

    def test_a_duty_is_never_scored_by_a_process_that_cannot_see_it(self):
        # The honesty column: entering a duty must not make the matrix imply that placement or
        # clock-in consults it, because nothing does yet.
        from . import views
        rows={row["name"]:row for row in views._compliance_matrix(self.org)}
        self.assertEqual(rows["Commercial general liability coverage"]["enforcement"],"Queue only")
        self.assertIn("Enforced at assignment",rows["Guard registration"]["measured"])

    # -- measuring it --

    def test_a_company_duty_with_no_current_certificate_needs_action(self):
        section=self.duties()
        self.assertEqual(section["total"],1)
        self.assertEqual(section["attention"],1)
        self.assertEqual(section["rows"][0]["state"],"missing")

    def test_filing_the_certificate_closes_the_duty(self):
        self.file_certificate(expires_in=200)
        section=self.duties()
        self.assertEqual(section["attention"],0)
        self.assertEqual(section["rows"][0]["state"],"active")

    def test_a_certificate_expiring_inside_the_renewal_window_is_reported_early(self):
        # 30 days out against a 45-day lead: the point of the window is that the operator has the
        # notice before the certificate is gone, not after.
        self.file_certificate(expires_in=30)
        section=self.duties()
        self.assertEqual(section["rows"][0]["state"],"pending")
        self.assertEqual(section["attention"],1)

    def test_an_expired_certificate_is_reported_as_expired_not_missing(self):
        self.file_certificate(expires_in=-5)
        self.assertEqual(self.duties()["rows"][0]["state"],"expired")

    def test_an_officer_duty_rows_every_applicable_officer_and_no_other(self):
        screening=DocumentType.objects.create(organization=self.org,name="Firearms clearance",code="clearance",sensitivity=DocumentType.Sensitivity.RESTRICTED)
        ComplianceRule.objects.create(organization=self.org,name="Range qualification",code="range",
            evidence=ComplianceRule.Evidence.DOCUMENT,applies_to_subject=ComplianceRule.Subject.PEOPLE,applies_to=["commissioned"],
            document_type=screening,jurisdiction="Texas",authority_url="https://www.dps.texas.gov/section/private-security",
            authority_reference="37 TAC §35.8",interpretation="Each armed officer qualifies annually.",effective_from=self.today,
            warning_days=45,approved_by=self.owner,approved_at=self.timezone.now())
        section=self.duties()
        names=[row["person"].full_name for row in section["rows"] if row["person"]]
        self.assertEqual(names,["Ray Ortiz"])
        self.assertEqual(section["total"],2)

    def test_a_duty_the_product_cannot_measure_is_listed_and_never_scored(self):
        posting=ComplianceRule.objects.create(organization=self.org,name="Posted notice of registration",code="posting",
            evidence=ComplianceRule.Evidence.POSTING,applies_to_subject=ComplianceRule.Subject.SITES,
            jurisdiction="Texas",authority_url="https://statutes.capitol.texas.gov/Docs/OC/htm/OC.1702.htm",
            authority_reference="§1702.128",interpretation="Posted at each location.",effective_from=self.today,
            approved_by=self.owner,approved_at=self.timezone.now())
        section=self.duties()
        row=[item for item in section["rows"] if item["subject"]==posting.name][0]
        self.assertEqual(row["state"],"not-evaluated")
        self.assertIn("records cannot be filed against a site yet",row["note"])
        self.assertEqual(section["total"],1, "an unmeasured duty must not enter the denominator")
        self.assertEqual(section["unmeasured"],1)

    def test_an_unapproved_duty_is_not_enforced_yet(self):
        self.duty.approved_at=None; self.duty.approved_by=None; self.duty.save()
        self.assertIn("not approved",self.duty.unevaluated_reason)
        self.assertEqual(self.duties()["total"],0)

    def test_posting_duties_are_unmeasured_for_company_and_people_with_or_without_a_record_type(self):
        for subject in (ComplianceRule.Subject.ORGANIZATION, ComplianceRule.Subject.PEOPLE):
            for document_type in (None, self.certificate):
                with self.subTest(subject=subject, document_type=document_type):
                    self.duty.evidence = ComplianceRule.Evidence.POSTING
                    self.duty.applies_to_subject = subject
                    self.duty.applies_to = ["commissioned"]
                    self.duty.document_type = document_type
                    self.duty.save()

                    section = self.duties()
                    self.assertEqual(section["total"], 0)
                    self.assertEqual(section["attention"], 0)
                    self.assertEqual(section["unmeasured"], 1)
                    self.assertEqual(section["rows"][0]["state"], "not-evaluated")
                    self.assertIn("posting evidence is not tracked yet", section["rows"][0]["note"])
                    page = self.client.get(reverse("compliance"), {"person": self.commissioned.pk})
                    self.assertEqual(page.status_code, 200)

    # -- the ladder still applies to duties --

    def test_a_duty_whose_evidence_is_sealed_from_the_reader_says_not_measured_not_missing(self):
        """"No certificate filed" is a false statement when it is filed and the reader may not see it."""
        self.file_certificate(expires_in=200)
        self.certificate.sensitivity=DocumentType.Sensitivity.RESTRICTED; self.certificate.save()
        auditor=Membership.objects.create(user=self.User.objects.create_user(username="duty-auditor@example.com",password="correct horse battery staple"),
            organization=self.org,role=Membership.Role.AUDITOR)
        section=self.duties(auditor)
        self.assertEqual(section["rows"][0]["state"],"not-evaluated")
        self.assertIn("your role does not open the record type",section["rows"][0]["note"])
        self.assertEqual(section["total"],0)

    def test_the_owner_who_files_a_restricted_certificate_still_sees_the_duty(self):
        # Both halves of the previous test: the same row must not read as unmeasured for the role
        # that can actually open the evidence, or the queue would hide the company's own duty.
        self.file_certificate(expires_in=200)
        self.certificate.sensitivity=DocumentType.Sensitivity.RESTRICTED; self.certificate.save()
        self.assertEqual(self.duties()["rows"][0]["state"],"active")

    def test_a_supervisor_can_open_the_duty_tab_without_reading_the_personnel_file(self):
        supervisor=self.User.objects.create_user(username="duty-supervisor@example.com",password="correct horse battery staple")
        Membership.objects.create(user=supervisor,organization=self.org,role=Membership.Role.SUPERVISOR)
        self.client.force_login(supervisor)
        page=self.client.get(reverse("compliance")+"?kind=duties")
        self.assertEqual(page.status_code,200)
        self.assertContains(page,"Duties and what is measured")

    # -- versions and tenancy --

    def test_renaming_a_duty_does_not_open_a_new_version(self):
        response=self.client.post(reverse("compliance_rule_edit",args=[self.duty.pk]),{"name":"CGL coverage at statutory limits","code":"cgl-hold",
            "evidence":self.duty.evidence,"applies_to_subject":self.duty.applies_to_subject,"document_type":self.certificate.pk,
            "required_hours":"","jurisdiction":"Texas","authority_url":self.duty.authority_url,"authority_reference":self.duty.authority_reference,
            "interpretation":self.duty.interpretation,"effective_from":self.duty.effective_from.isoformat(),"effective_until":"",
            "warning_days":"45","reminder_days_before":"","active":"on"})
        self.assertRedirects(response,reverse("settings_compliance"))
        self.duty.refresh_from_db()
        self.assertEqual(self.duty.revision,1,"a label retyped is not a rule changed")
        self.assertEqual(RuleRevision.objects.filter(kind=RuleRevision.Kind.COMPLIANCE_RULE,rule_id=str(self.duty.pk)).count(),1)

    def test_changing_the_renewal_window_opens_a_version_that_still_reads_as_itself(self):
        self.client.post(reverse("compliance_rule_edit",args=[self.duty.pk]),{"name":self.duty.name,"code":"cgl-hold",
            "evidence":self.duty.evidence,"applies_to_subject":self.duty.applies_to_subject,"document_type":self.certificate.pk,
            "required_hours":"","jurisdiction":"Texas","authority_url":self.duty.authority_url,"authority_reference":self.duty.authority_reference,
            "interpretation":self.duty.interpretation,"effective_from":self.duty.effective_from.isoformat(),"effective_until":"",
            "warning_days":"90","reminder_days_before":"","active":"on"})
        self.duty.refresh_from_db()
        self.assertEqual(self.duty.revision,2)
        history=RuleRevision.objects.filter(kind=RuleRevision.Kind.COMPLIANCE_RULE,rule_id=str(self.duty.pk)).order_by("revision")
        self.assertEqual([row.revision for row in history],[1,2])
        self.assertEqual(history[0].values["warning_days"],45,"the window a reminder was sent against must be readable after the edit")

    def test_the_rule_history_screen_lists_duty_versions(self):
        page=self.client.get(reverse("rule_history"))
        self.assertContains(page,"Compliance duty")
        self.assertContains(page,"Commercial general liability coverage")

    def test_another_company_cannot_see_or_edit_this_duty(self):
        other=Organization.objects.create(legal_name="Other LLC",display_name="Other",slug="other-llc")
        outsider=self.User.objects.create_user(username="duty-outsider@example.com",password="correct horse battery staple")
        Membership.objects.create(user=outsider,organization=other,role=Membership.Role.OWNER)
        self.client.force_login(outsider)
        self.assertNotContains(self.client.get(reverse("settings_compliance")),"Commercial general liability coverage")
        self.assertEqual(self.client.get(reverse("compliance_rule_edit",args=[self.duty.pk])).status_code,404)
        foreign_type=DocumentType.objects.create(organization=other,name="Foreign type",code="foreign-type")
        self.client.post(reverse("compliance_rule_create"),{"name":"Foreign duty","code":"foreign-duty","evidence":ComplianceRule.Evidence.DOCUMENT,
            "applies_to_subject":ComplianceRule.Subject.ORGANIZATION,"document_type":foreign_type.pk,"required_hours":"","jurisdiction":"Texas",
            "authority_url":"https://example.com","authority_reference":"x","interpretation":"x","effective_from":"","effective_until":"",
            "warning_days":"60","reminder_days_before":"","active":"on"})
        self.assertFalse(ComplianceRule.objects.filter(organization=self.org,code="foreign-duty").exists())


class NotificationEventFamiliesTest(TestCase):
    """NTF-3: the event families RB §Notification event catalog asks for, wired to real state changes.

    The catalogue's six families were only partly covered: invitations, schedule changes, time off,
    credential and training reminders, and acknowledgment requests. Missing was the whole timekeeping
    family, payroll state, retention and legal-hold notices, and operations. The defect class is the
    one the benchmark already found for credentials — *a queue built from rows only notices what
    somebody filed a row about* — so the missed-punch pass is the load-bearing test here: it reports
    the gap that produces no punch row at all.
    """

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User=get_user_model()
        self.timedelta=timedelta
        self.timezone=timezone
        self.owner=User.objects.create_user(username="ntf-owner@example.com",password="correct horse battery staple")
        self.admin=User.objects.create_user(username="ntf-admin@example.com",password="correct horse battery staple")
        self.hr=User.objects.create_user(username="ntf-hr@example.com",password="correct horse battery staple")
        self.payroll=User.objects.create_user(username="ntf-payroll@example.com",password="correct horse battery staple")
        self.supervisor=User.objects.create_user(username="ntf-supervisor@example.com",password="correct horse battery staple")
        self.officer_user=User.objects.create_user(username="ntf-officer@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Notice LLC",display_name="Notice",slug="notice-llc")
        for user,role in ((self.owner,Membership.Role.OWNER),(self.admin,Membership.Role.ADMIN),
                          (self.hr,Membership.Role.HR),(self.payroll,Membership.Role.PAYROLL),
                          (self.supervisor,Membership.Role.SUPERVISOR),(self.officer_user,Membership.Role.OFFICER)):
            Membership.objects.create(user=user,organization=self.org,role=role)
        self.officer=Person.objects.create(organization=self.org,user=self.officer_user,first_name="Ana",last_name="Delgado",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        # Deliberately not named `client`: TestCase.client is the HTTP test client and shadowing it
        # here would break every `self.client.force_login` below.
        self.contract=Client.objects.create(organization=self.org,name="Reception Group")
        # No coordinates: the geofence rule cannot fire, so each test below controls its own exception.
        self.site=Site.objects.create(organization=self.org,client=self.contract,name="Lone Star Tower",address="1 Test St")
        self.now=timezone.now().replace(microsecond=0)
        self.post=Shift.objects.create(organization=self.org,site=self.site,officer=self.officer,
            starts_at=self.now-self.timedelta(days=2),ends_at=self.now-self.timedelta(days=2,hours=8),
            status=Shift.Status.PUBLISHED)

    def punch(self,kind,occurred_at,shift=None,offline=False):
        from uuid import uuid4
        from .services import record_punch
        return record_punch(organization=self.org,person=self.officer,client_event_id=uuid4(),kind=kind,
            occurred_at=occurred_at,shift=shift,offline=offline,actor=self.officer_user,source="mobile")

    def notices(self,event_type,user=None):
        rows=Notification.objects.filter(organization=self.org,event_type=event_type)
        return rows if user is None else rows.filter(recipient=user)

    # -- timekeeping --

    def test_a_punch_exception_reaches_the_roles_that_review_it_and_not_the_officer(self):
        # A clock-out with no matching clock-in is the exception this path can produce on its own:
        # the punch *is* linked to a post, so nothing about the link is at fault. The officer stood
        # there and does not need telling; the reviewer does.
        self.punch(Punch.Kind.OUT,self.now-self.timedelta(minutes=20),shift=self.post)
        rows=self.notices("punch.exception")
        self.assertTrue(rows.exists())
        recipients={row.recipient_id for row in rows}
        self.assertIn(self.owner.id,recipients)
        self.assertIn(self.supervisor.id,recipients)
        self.assertNotIn(self.officer_user.id,recipients)

    def test_a_resynchronised_punch_does_not_notice_the_exception_twice(self):
        # The device retries its own upload; the second delivery carries the same client event id and
        # must not double-mail the reviewer.
        from uuid import uuid4
        from .services import record_punch
        event_id=uuid4()
        punch,_=record_punch(organization=self.org,person=self.officer,client_event_id=event_id,kind=Punch.Kind.IN,
            occurred_at=self.now-self.timedelta(hours=2),shift=self.post,actor=self.officer_user)
        first=self.notices("punch.exception").count()
        again,created=record_punch(organization=self.org,person=self.officer,client_event_id=event_id,kind=Punch.Kind.IN,
            occurred_at=self.now-self.timedelta(hours=2),shift=self.post,actor=self.officer_user)
        self.assertFalse(created)
        self.assertEqual(again.pk,punch.pk)
        self.assertEqual(self.notices("punch.exception").count(),first)

    def test_a_clean_pair_of_punches_notifies_nobody(self):
        # Linked to a post and matched in/out, so there is no exception to report. A notice for every
        # accepted punch is how a channel gets muted.
        worked=Shift.objects.create(organization=self.org,site=self.site,officer=self.officer,
            starts_at=self.now-self.timedelta(hours=4),ends_at=self.now-self.timedelta(hours=1),
            status=Shift.Status.PUBLISHED)
        self.punch(Punch.Kind.IN,worked.starts_at+self.timedelta(minutes=5),shift=worked)
        self.punch(Punch.Kind.OUT,worked.ends_at-self.timedelta(minutes=5),shift=worked)
        self.assertEqual(self.notices("punch.exception").count(),0)
        self.assertEqual(Notification.objects.filter(organization=self.org).count(),0)

    def test_an_offline_punch_tells_the_reviewer_what_it_is_not(self):
        # The exception reason says "not linked to a scheduled shift"; only the offline flag says the
        # evidence arrived from a device that may have been off signal for hours.
        punch,created=self.punch(Punch.Kind.IN,self.now-self.timedelta(hours=2),offline=True)
        row=self.notices("punch.exception").first()
        self.assertIn("offline device",row.body)

    def test_a_closed_post_with_no_punch_at_all_is_noticed(self):
        from .services import queue_missing_punch_reports
        queued=queue_missing_punch_reports(now=self.now)
        self.assertGreaterEqual(queued,1)
        row=self.notices("punch.missing",self.officer_user).first()
        self.assertIsNotNone(row,"the officer is the only person who can confirm the hours were not worked")
        self.assertIn("clock-in",row.subject)
        self.assertIn("clock-out",row.subject)
        self.assertEqual(queue_missing_punch_reports(now=self.now),0,"the daily pass must not re-chase the same gap")

    def test_a_fully_punched_post_is_never_reported_as_missing(self):
        # The passes that close the sync window have to be older than record_punch will accept, so
        # the pair is filed directly: this is testing the gap detector, not the clock.
        from uuid import uuid4
        from .services import queue_missing_punch_reports
        Punch.objects.create(organization=self.org,person=self.officer,shift=self.post,client_event_id=uuid4(),
            kind=Punch.Kind.IN,occurred_at=self.post.starts_at+self.timedelta(minutes=5))
        Punch.objects.create(organization=self.org,person=self.officer,shift=self.post,client_event_id=uuid4(),
            kind=Punch.Kind.OUT,occurred_at=self.post.ends_at-self.timedelta(minutes=5))
        self.assertEqual(queue_missing_punch_reports(now=self.now),0)

    def test_a_post_that_only_just_closed_is_left_alone_while_the_device_can_still_sync(self):
        from .services import PUNCH_MISSING_GRACE_MINUTES, queue_missing_punch_reports
        recent=Shift.objects.create(organization=self.org,site=self.site,officer=self.officer,
            starts_at=self.now-self.timedelta(hours=3),ends_at=self.now-self.timedelta(hours=1),
            status=Shift.Status.PUBLISHED)
        queue_missing_punch_reports(now=self.now)
        self.assertFalse(self.missing_for(recent),
                         "a post twelve hours inside the sync window is a late upload, not a gap")
        queue_missing_punch_reports(now=self.now+self.timedelta(minutes=PUNCH_MISSING_GRACE_MINUTES+1))
        self.assertTrue(self.missing_for(recent))

    def missing_for(self,shift):
        return [row for row in self.notices("punch.missing") if row.deduplication_key.startswith(f"punch.missing:{shift.pk}:")]

    def test_a_correction_request_reaches_the_reviewer_and_the_answer_reaches_the_officer(self):
        punch,_=self.punch(Punch.Kind.IN,self.now-self.timedelta(hours=4))
        self.client.force_login(self.officer_user)
        response=self.client.post(reverse("adjustment_request",args=[punch.pk]),
            {"proposed_at":(punch.occurred_at+self.timedelta(minutes=30)).isoformat(),"reason":"Briefing ran long, I clocked in late."})
        self.assertRedirects(response,reverse("clock"))
        item=PunchAdjustment.objects.get(punch=punch)
        reviewers={row.recipient_id for row in self.notices("punch.correction_requested")}
        self.assertIn(self.payroll.id,reviewers)
        self.assertNotIn(self.officer_user.id,reviewers)
        self.client.force_login(self.payroll)
        self.client.post(reverse("adjustment_review",args=[item.pk]),{"action":"approved","note":"Supervisor confirmed the time"})
        answered=self.notices("punch.correction_approved",self.officer_user).first()
        self.assertIsNotNone(answered)
        self.assertIn("Supervisor confirmed",answered.body)

    # -- payroll state --

    def test_locking_and_reopening_payroll_reach_the_roles_that_stood_behind_the_number(self):
        import uuid
        from .services import create_payroll_run
        # Filed directly and accepted: a punch in review would raise a payroll exception, and
        # approval refuses while one is open — which is the right behaviour, not this test's subject.
        Punch.objects.create(organization=self.org,person=self.officer,client_event_id=uuid.uuid4(),
            kind=Punch.Kind.IN,occurred_at=self.now-self.timedelta(days=1,hours=8))
        Punch.objects.create(organization=self.org,person=self.officer,client_event_id=uuid.uuid4(),
            kind=Punch.Kind.OUT,occurred_at=self.now-self.timedelta(days=1,hours=16))
        run=create_payroll_run(organization=self.org,start=self.now-self.timedelta(days=2),end=self.now,actor=self.payroll)
        self.client.force_login(self.payroll)
        self.client.post(reverse("payroll_approve",args=[run.pk]))
        locked=self.notices("payroll.locked")
        self.assertTrue(locked.exists())
        recipients={row.recipient_id for row in locked}
        self.assertNotIn(self.payroll.id,recipients,"the actor pressed the button")
        self.assertIn(self.owner.id,recipients)
        # Reopening needs the company's own switch, and an owner rather than the approver who locked it.
        policy,_=TimePolicy.objects.get_or_create(organization=self.org)
        policy.allow_reopen=True;policy.save()
        self.client.force_login(self.owner)
        response=self.client.post(reverse("payroll_reopen",args=[run.pk]),{"reason":"A late correction changes three rows in this period."})
        self.assertRedirects(response,reverse("payroll")+f"?run={run.pk}")
        reopened=self.notices("payroll.reopened")
        self.assertTrue(reopened.exists(),"the people who relied on the locked figure must hear it moved")
        self.assertIn("late correction",reopened.first().body)

    def test_exporting_payroll_records_readiness_for_everyone_who_did_not_press_the_button(self):
        from .services import create_payroll_run
        run=create_payroll_run(organization=self.org,start=self.now-self.timedelta(days=2),end=self.now,actor=self.payroll)
        run.status=PayrollRun.Status.APPROVED;run.approved_by=self.payroll;run.approved_at=self.now;run.save()
        self.client.force_login(self.payroll)
        self.assertEqual(self.client.get(reverse("payroll_run_export",args=[run.pk])+"?format=csv").status_code,200)
        recipients={row.recipient_id for row in self.notices("payroll.exported")}
        self.assertNotIn(self.payroll.id,recipients)
        self.assertIn(self.owner.id,recipients)

    # -- retention and holds --

    def file_document(self,kind,person=None,name="record.pdf"):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        if not getattr(self,"_media_started",None):
            media=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
            override=override_settings(MEDIA_ROOT=media.name); override.enable()
            self.addCleanup(override.disable); self.addCleanup(media.cleanup)
            self._media_started=True
        return PersonDocument.objects.create(organization=self.org,person=person,document_type=kind,
            file=SimpleUploadedFile(name,b"%PDF-1.4\nx",content_type="application/pdf"),original_name=name,
            content_type="application/pdf",size=11,sha256="0"*64,scan_status=PersonDocument.ScanStatus.CLEAN)

    def test_a_legal_hold_notice_reaches_the_desk_that_works_the_retention_queue(self):
        kind=DocumentType.objects.create(organization=self.org,name="Incident report",code="incident",retention_days=30)
        document=self.file_document(kind,person=self.officer,name="report.pdf")
        self.client.force_login(self.owner)
        self.client.post(reverse("legal_hold_toggle",args=[document.pk]))
        rows=self.notices("retention.hold_changed")
        recipients={row.recipient_id for row in rows}
        self.assertIn(self.hr.id,recipients)
        self.assertIn("Legal hold applied",rows.first().subject)
        self.client.post(reverse("legal_hold_toggle",args=[document.pk]))
        subjects={row.subject for row in self.notices("retention.hold_changed")}
        self.assertEqual(len(subjects),2,"release is its own notice, not a silent edit")
        self.assertIn("Legal hold released: report.pdf",subjects)

    def test_a_disposition_request_notifies_the_second_approver_and_the_decision_notifies_the_requester(self):
        kind=DocumentType.objects.create(organization=self.org,name="Old contract",code="old-contract",retention_days=1)
        document=self.file_document(kind,name="contract.pdf")
        self.client.force_login(self.owner)
        self.client.post(reverse("disposition_request",args=[document.pk]),{"action":"delete","reason":"Retention date passed and no hold is on the record."})
        request=DispositionRequest.objects.get(document=document)
        asked={row.recipient_id for row in self.notices("retention.disposition_requested")}
        self.assertIn(self.admin.id,asked)
        self.assertNotIn(self.owner.id,asked,"a request cannot notify its own author for approval")
        self.client.force_login(self.admin)
        self.client.post(reverse("disposition_execute",args=[request.pk]))
        decided=self.notices("retention.disposition_executed",self.owner)
        self.assertTrue(decided.exists(),"the requester is the only party without a screen to check")

    # -- operations, and the two invariants every notice has to hold --

    def test_an_import_says_how_many_rows_it_actually_applied(self):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .services import preview_csv_import
        payload=b"first_name,last_name,email,status\nAvery,Nguyen,avery-ntf@example.com,active\n"
        batch,_=preview_csv_import(organization=self.org,entity="people",
            upload=SimpleUploadedFile("people.csv",payload,content_type="text/csv"),actor=self.hr)
        self.client.force_login(self.hr)
        self.client.post(reverse("import_apply",args=[batch.pk]))
        row=self.notices("import.completed",self.hr).first()
        self.assertIn("1 people row(s)",row.subject)
        self.assertIn("of 1 parsed",row.body)

    def test_no_new_notice_is_queued_without_a_deduplication_key(self):
        # The unique constraint is conditional on a non-empty key, so a blank key silently turns the
        # whole re-send guard off. Every family above is asserted through this instead of per call site.
        from .services import queue_missing_punch_reports
        self.punch(Punch.Kind.OUT,self.now-self.timedelta(minutes=20),shift=self.post)
        queue_missing_punch_reports(now=self.now)
        self.assertTrue(Notification.objects.filter(organization=self.org).exists())
        self.assertEqual(Notification.objects.filter(organization=self.org,deduplication_key="").count(),0)

    def test_no_notice_leaks_to_the_sms_channel_before_consent_exists(self):
        # NTF-4 is a production gate, so nothing may select SMS until consent and opt-out land.
        from .services import queue_missing_punch_reports
        self.punch(Punch.Kind.OUT,self.now-self.timedelta(minutes=20),shift=self.post)
        queue_missing_punch_reports(now=self.now)
        self.assertTrue(Notification.objects.filter(organization=self.org).exists())
        self.assertEqual(Notification.objects.filter(organization=self.org,channel=Notification.Channel.SMS).count(),0)


class ReportSnapshotTest(TestCase):
    """RPT-1/2/3: a figure has to be stored to be answerable later, and split to be actionable now.

    The reports page computed three numbers and threw them away. Filing the lapsed registration
    tomorrow changes what recomputing 12 June would say, so "were we compliant last month" was
    unanswerable in principle — and a single company percentage could not tell a branch manager
    which part of the company was the problem. The invariant tests below are the ones that catch a
    recompute-instead-of-read, a breakdown that stops adding up, and a scoped manager seeing a
    subject that is not theirs.
    """

    def setUp(self):
        import uuid
        from datetime import timedelta
        from django.utils import timezone
        User=get_user_model()
        self.User=User
        self.timedelta=timedelta
        self.timezone=timezone
        self.uuid=uuid
        self.today=timezone.localdate()
        self.now=timezone.now().replace(microsecond=0)
        self.owner=User.objects.create_user(username="rpt-owner@example.com",password="correct horse battery staple")
        self.supervisor=User.objects.create_user(username="rpt-supervisor@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Ledger LLC",display_name="Ledger",slug="ledger-llc")
        self.owner_membership=Membership.objects.create(user=self.owner,organization=self.org,role=Membership.Role.OWNER)
        self.sup_membership=Membership.objects.create(user=self.supervisor,organization=self.org,role=Membership.Role.SUPERVISOR)
        self.north=Branch.objects.create(organization=self.org,name="North Texas",city="Denton")
        self.south=Branch.objects.create(organization=self.org,name="South Texas",city="Waco")
        self.hillcrest=Client.objects.create(organization=self.org,name="Hillcrest Group")
        self.riverside=Client.objects.create(organization=self.org,name="Riverside")
        self.site_north=Site.objects.create(organization=self.org,client=self.hillcrest,branch=self.north,name="Hillcrest Gate",address="1 North St")
        self.site_south=Site.objects.create(organization=self.org,client=self.riverside,branch=self.south,name="Riverside Tower",address="2 South St")
        self.anna=Person.objects.create(organization=self.org,branch=self.north,first_name="Ana",last_name="Delgado",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        self.bo=Person.objects.create(organization=self.org,branch=self.south,first_name="Beth",last_name="Cole",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        # No branch on the row: the person a breakdown is most likely to lose is the one nobody filed.
        self.drift=Person.objects.create(organization=self.org,first_name="Cal",last_name="Diaz",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        self.requirement=CredentialType.objects.create(organization=self.org,name="Guard registration",code="guard-reg",
            applies_to=["unarmed"],blocks_scheduling=True,jurisdiction="Texas",
            authority_url="https://statutes.capitol.texas.gov/Docs/OC/htm/OC.1702.htm",authority_reference="§1702.302",
            interpretation="Every unarmed officer holds a current registration.",effective_from=self.today-self.timedelta(days=100),
            approved_by=self.owner,approved_at=self.now)
        Credential.objects.create(organization=self.org,person=self.anna,credential_type=self.requirement,
            status=Credential.Status.ACTIVE,expires_on=self.today+self.timedelta(days=300))
        # A post is "at risk" against the requirements its own contract or site declares, not the
        # company-wide applicable set — so the south site has to name the registration for Beth's
        # missing one to be a coverage problem rather than only a compliance one.
        self.site_south.required_credentials.add(self.requirement)
        # Coverage: one eligible officer, one assigned but not eligible, one nobody, one draft.
        # The draft is deliberately unassigned — a draft that shared an officer and a time with the
        # eligible post would make that post read as double-booked and quietly break the fixture.
        self.post_ok=self.shift(self.site_north,self.anna,Shift.Status.PUBLISHED)
        self.post_risky=self.shift(self.site_south,self.bo,Shift.Status.PUBLISHED)
        self.post_open=self.shift(self.site_south,None,Shift.Status.PUBLISHED)
        self.post_draft=self.shift(self.site_north,None,Shift.Status.DRAFT)
        # Tours: one closed with both punches, one that ended with none.
        self.tour_closed=self.shift(self.site_north,self.anna,Shift.Status.PUBLISHED,ended=True)
        self.tour_open=self.shift(self.site_south,self.bo,Shift.Status.PUBLISHED,ended=True)
        for kind in (Punch.Kind.IN,Punch.Kind.OUT):
            Punch.objects.create(organization=self.org,person=self.anna,shift=self.tour_closed,client_event_id=uuid.uuid4(),
                kind=kind,occurred_at=self.tour_closed.starts_at+self.timedelta(hours=4 if kind==Punch.Kind.OUT else 0))
        self.client.force_login(self.owner)

    def shift(self,site,officer,status,ended=False):
        if ended:
            starts=self.now-self.timedelta(days=1,hours=8); ends=self.now-self.timedelta(days=1)
        else:
            starts=self.now+self.timedelta(days=1); ends=starts+self.timedelta(hours=8)
        return Shift.objects.create(organization=self.org,site=site,officer=officer,starts_at=starts,ends_at=ends,
                                    status=status,post_name="Gate house" if site==self.site_north else "Tower lobby")

    def figures(self,membership=None):
        from .scope import for_membership
        from .services import report_figures
        return report_figures(self.org,for_membership(membership or self.owner_membership),today=self.today)

    def stored(self,metric="compliance",subject="company",period=None):
        return ReportSnapshot.objects.get(organization=self.org,metric=metric,subject_key=subject,
                                          period_date=period or self.today)

    # -- RPT-1: storing the figure --

    def test_a_capture_stores_one_row_per_subject_and_measure(self):
        from .services import capture_report_snapshots
        created=capture_report_snapshots(self.org,period_date=self.today)
        self.assertEqual(created,15)  # company + 2 branches + 2 contracts, across three measures
        subjects={row.subject_key for row in ReportSnapshot.objects.filter(organization=self.org)}
        self.assertEqual(subjects,{"company",f"branch:{self.north.pk}",f"branch:{self.south.pk}",
                                   f"client:{self.hillcrest.pk}",f"client:{self.riverside.pk}"})

    def test_a_stored_figure_is_the_number_the_panel_showed_at_that_moment(self):
        # RPT-4 as a test: the snapshot and the live panel are the same computation, so a stored
        # figure cannot be the one that quietly drifts from the queue somebody is working.
        from .services import capture_report_snapshots
        live=self.figures()
        page=self.client.get(reverse("reports")).context
        self.assertEqual(page["summary"]["total"],live["compliance"]["total"])
        self.assertEqual(page["summary"]["rate"],live["compliance"]["rate"])
        capture_report_snapshots(self.org,period_date=self.today)
        row=self.stored()
        # The stored column is a Decimal and the computation returns a float; compared as floats,
        # because a rate that rounds differently on the way into the database is the bug here.
        self.assertEqual((row.total,row.satisfied,row.attention,float(row.rate)),
                         (live["compliance"]["total"],live["compliance"]["satisfied"],
                          live["compliance"]["attention"],live["compliance"]["rate"]))

    def test_fixing_the_evidence_later_does_not_move_the_stored_figure(self):
        from .services import capture_report_snapshots
        capture_report_snapshots(self.org,period_date=self.today)
        before=self.stored()
        self.assertEqual(before.attention,2)  # Beth and Cal have no registration
        Credential.objects.create(organization=self.org,person=self.bo,credential_type=self.requirement,
            status=Credential.Status.ACTIVE,expires_on=self.today+self.timedelta(days=300))
        self.assertEqual(capture_report_snapshots(self.org,period_date=self.today),0,
                         "a day that is already stored is not re-measured")
        after=self.stored()
        self.assertEqual(after.attention,2,"the record has to say what was true then")
        self.assertEqual(self.figures()["compliance"]["attention"],1,
                         "and the live figure must have moved, or this test proves nothing")

    def test_the_basis_records_what_the_number_counted_and_what_it_left_out(self):
        from .services import capture_report_snapshots
        capture_report_snapshots(self.org,period_date=self.today)
        basis=self.stored().basis
        self.assertIn("company-wide",basis["reader_basis"])
        self.assertEqual(sorted(basis["kinds"]),["credentials","documents","duties","training"],
                         "a rate must say which kinds it divided, and the document section is one of them")
        self.assertEqual(self.stored("coverage").basis["drafts_excluded"],1,
                         "a draft is excluded, and the exclusion is stated rather than invisible")

    def test_an_empty_window_is_stored_as_none_rather_than_zero(self):
        from .services import capture_report_snapshots
        Shift.objects.filter(organization=self.org).delete()
        capture_report_snapshots(self.org,period_date=self.today)
        row=self.stored("coverage")
        self.assertIsNone(row.rate)
        self.assertEqual(row.total,0)
        page=self.client.get(reverse("saved_reports")).content.decode()
        self.assertIn("nothing was measured",page)

    def test_the_items_behind_the_number_are_stored_with_it(self):
        from .services import capture_report_snapshots
        capture_report_snapshots(self.org,period_date=self.today)
        names={item["person"] for item in self.stored().exceptions}
        self.assertEqual(names,{"Beth Cole","Cal Diaz"})
        coverage=self.stored("coverage").exceptions
        risky={item["problem"][:16] for item in coverage}
        self.assertIn("assigned officer", risky, "an ineligible officer is a different problem from an empty post")
        self.assertEqual(len(coverage),2)

    def test_a_duty_that_is_not_measured_cannot_inflate_the_breakdown(self):
        """The split has to add up to the panel, and unmeasured rows are in the queue but not the rate."""
        from .services import capture_report_snapshots, report_breakdown
        ComplianceRule.objects.create(organization=self.org,name="Posted notice of registration",code="posting",
            evidence=ComplianceRule.Evidence.POSTING,applies_to_subject=ComplianceRule.Subject.SITES,
            jurisdiction="Texas",authority_url="https://statutes.capitol.texas.gov/Docs/OC/htm/OC.1702.htm",
            authority_reference="§1702.128",interpretation="Posted at each location.",effective_from=self.today,
            approved_by=self.owner,approved_at=self.now)
        page=self.client.get(reverse("reports")).context
        breakdown=report_breakdown(self.org,page["attendance"],page["coverage"],page["closed"])
        parts=page["summary"]
        self.assertEqual(sum(line["total"] for line in breakdown["compliance"]["branch"]),parts["total"],
                         "groups that do not sum to the number above them are a second definition")
        self.assertEqual(sum(line["attention"] for line in breakdown["compliance"]["branch"]),parts["attention"])

    # -- RPT-3: the breakdown --

    def test_a_person_with_no_branch_recorded_still_counts_in_the_split(self):
        from .services import report_breakdown
        page=self.client.get(reverse("reports")).context
        lines={line["subject"]:line for line in report_breakdown(self.org,page["attendance"],page["coverage"],page["closed"])["compliance"]["branch"]}
        self.assertIn("Branch not recorded",lines)
        self.assertEqual(lines["Branch not recorded"]["total"],1)
        self.assertEqual(lines["Branch not recorded"]["attention"],1)

    def test_obligations_split_by_branch_and_coverage_splits_by_contract_too(self):
        from .services import report_breakdown
        page=self.client.get(reverse("reports")).context
        breakdown=report_breakdown(self.org,page["attendance"],page["coverage"],page["closed"])
        self.assertEqual(breakdown["compliance"]["contract"],[])
        self.assertIn("attach to a person",breakdown["compliance"]["note"],
                      "no client split for an obligation is a stated reason, not a missing feature")
        clients={line["subject"] for line in breakdown["coverage"]["contract"]}
        self.assertEqual(clients,{"Hillcrest Group","Riverside"})
        self.assertEqual(next(line for line in breakdown["coverage"]["contract"] if line["subject"]=="Riverside")["attention"],2)

    def test_the_tour_split_uses_the_posts_that_actually_closed(self):
        from .services import report_breakdown
        page=self.client.get(reverse("reports")).context
        lines={line["subject"]:line for line in report_breakdown(self.org,page["attendance"],page["coverage"],page["closed"])["tour"]["branch"]}
        # Only the two posts that had ended are tours; the forward-dated posts, draft included, are
        # coverage and must not be counted twice under a different measure.
        self.assertEqual(lines["North Texas"]["total"],1)
        self.assertEqual(lines["North Texas"]["satisfied"],1)
        self.assertEqual(lines["South Texas"]["attention"],1)

    # -- RPT-2: history and scope --

    def test_two_periods_are_both_kept_and_read_back_newest_first(self):
        from .services import capture_report_snapshots
        capture_report_snapshots(self.org,period_date=self.today-self.timedelta(days=1))
        capture_report_snapshots(self.org,period_date=self.today)
        rows=self.client.get(reverse("reports")).context["history"]["compliance"]
        self.assertEqual([row.period_date for row in rows][:1],[self.today],"newest first")
        self.assertEqual({row.subject_key for row in rows if row.period_date==self.today},
                         {"company",f"branch:{self.north.pk}",f"branch:{self.south.pk}",
                          f"client:{self.hillcrest.pk}",f"client:{self.riverside.pk}"})

    def test_the_history_shows_stored_figures_and_not_a_recomputation(self):
        from .services import capture_report_snapshots
        capture_report_snapshots(self.org,period_date=self.today)
        stored=self.stored()
        Credential.objects.create(organization=self.org,person=self.bo,credential_type=self.requirement,
            status=Credential.Status.ACTIVE,expires_on=self.today+self.timedelta(days=300))
        history=self.client.get(reverse("reports")).context["history"]["compliance"]
        company=[row for row in history if row.subject_key=="company"]
        self.assertEqual([float(row.rate) for row in company],[float(stored.rate)])
        self.assertNotEqual(float(stored.rate),self.figures()["compliance"]["rate"],
                            "if the stored number equalled today's, this test could not tell them apart")

    def grant(self,branch=None,client=None,site=None):
        AuthorityScope.objects.create(membership=self.sup_membership,organization=self.org,
            branch=branch,client=client,site=site)

    def test_a_granted_manager_sees_only_their_own_subjects_history(self):
        from .services import capture_report_snapshots
        self.grant(branch=self.north)
        capture_report_snapshots(self.org,period_date=self.today)
        self.client.force_login(self.supervisor)
        history=self.client.get(reverse("reports")).context["history"]
        for metric,rows in history.items():
            self.assertTrue(rows,f"{metric} should have a stored figure for the branch")
            self.assertEqual({row.subject_key for row in rows},{f"branch:{self.north.pk}"})

    def test_a_grant_that_reaches_no_branch_or_contract_shows_no_saved_figures(self):
        from .services import capture_report_snapshots
        self.grant(site=self.site_north)
        capture_report_snapshots(self.org,period_date=self.today)
        self.client.force_login(self.supervisor)
        history=self.client.get(reverse("reports")).context["history"]
        self.assertEqual([row for rows in history.values() for row in rows],[],
                         "falling back to the company series here would show a manager numbers they never owned")

    def test_a_manager_cannot_download_a_figure_from_a_subject_that_is_not_theirs(self):
        from .services import capture_report_snapshots
        self.grant(branch=self.north)
        capture_report_snapshots(self.org,period_date=self.today)
        mine=self.stored(metric="compliance",subject=f"branch:{self.north.pk}")
        theirs=self.stored(metric="compliance",subject=f"branch:{self.south.pk}")
        company=self.stored()
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse("report_snapshot_download",args=[mine.pk])).status_code,200)
        self.assertEqual(self.client.get(reverse("report_snapshot_download",args=[theirs.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse("report_snapshot_download",args=[company.pk])).status_code,404)

    def test_a_scoped_manager_saves_no_company_rows_by_filtering_the_list(self):
        from .services import capture_report_snapshots
        self.grant(branch=self.north)
        capture_report_snapshots(self.org,period_date=self.today)
        self.client.force_login(self.supervisor)
        page=self.client.get(reverse("saved_reports")).content.decode()
        self.assertNotIn("Whole company",page)
        self.assertIn("North Texas",page)

    def test_another_company_cannot_see_or_download_stored_figures(self):
        from .services import capture_report_snapshots
        capture_report_snapshots(self.org,period_date=self.today)
        other=self.User.objects.create_user(username="rpt-outsider@example.com",password="correct horse battery staple")
        elsewhere=Organization.objects.create(legal_name="Else LLC",display_name="Else",slug="else-llc")
        Membership.objects.create(user=other,organization=elsewhere,role=Membership.Role.OWNER)
        self.client.force_login(other)
        self.assertNotContains(self.client.get(reverse("saved_reports")),"Guard registration")
        self.assertEqual(self.client.get(reverse("report_snapshot_download",
            args=[self.stored().pk])).status_code,404)

    # -- the surface itself --

    def test_a_saved_figure_downloads_with_its_items_and_is_audited(self):
        from .services import capture_report_snapshots
        capture_report_snapshots(self.org,period_date=self.today)
        row=self.stored()
        response=self.client.get(reverse("report_snapshot_download",args=[row.pk]))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response["Content-Type"],"text/csv")
        body=response.content.decode()
        self.assertIn("Compliance rate",body)
        self.assertIn("Beth Cole",body)
        self.assertIn("company-wide",body)
        self.assertTrue(AuditEvent.objects.filter(action="report.exported",target_id=str(row.pk)).exists())

    def test_a_stored_note_that_looks_like_a_formula_is_neutralised_on_the_way_out(self):
        from .services import capture_report_snapshots
        capture_report_snapshots(self.org,period_date=self.today)
        row=self.stored()
        row.exceptions=[{"person":"=cmd|' /C calc'!A0","subject":"Guard registration","state":"missing","note":"+1"}]
        row.save()
        body=self.client.get(reverse("report_snapshot_download",args=[row.pk])).content.decode()
        self.assertIn("'=cmd",body)
        self.assertIn("'+1",body)

    def test_saving_today_is_owner_work_and_says_so_when_nothing_was_pinned(self):
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.post(reverse("report_capture")).status_code,403)
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.post(reverse("report_capture")),reverse("saved_reports"))
        second=self.client.post(reverse("report_capture"),follow=True)
        self.assertContains(second,"already saved",
                            msg_prefix="pressing it twice must not read like a new measurement: ")
        self.assertEqual(ReportSnapshot.objects.filter(organization=self.org).count(),15)

    def test_the_reports_page_links_the_history_and_the_saved_view(self):
        from .services import capture_report_snapshots
        capture_report_snapshots(self.org,period_date=self.today)
        page=self.client.get(reverse("reports"))
        self.assertContains(page,"Saved figures")
        self.assertContains(page,"By branch")
        self.assertContains(page,"Save today")
        self.assertContains(page,"Saved reports")

    def test_the_page_explains_the_gap_before_anything_is_stored(self):
        # The branch no other test renders: three panels, each with an empty history, and the page
        # has to say why the history is empty rather than showing three blank holes.
        page=self.client.get(reverse("reports"))
        self.assertEqual(page.status_code,200)
        self.assertContains(page,"Nothing saved yet",count=3)
        self.assertContains(page,"cannot be recovered")

    def test_a_worker_tick_on_a_stored_day_costs_nothing_but_an_existence_check(self):
        # The loop runs every minute. Recomputing three figures per subject 1,440 times a day to
        # create nothing would be a capacity problem the reporting feature itself caused.
        from .services import capture_report_snapshots
        capture_report_snapshots(self.org,period_date=self.today)
        with self.assertNumQueries(1):
            self.assertEqual(capture_report_snapshots(self.org,period_date=self.today),0)


class ScheduleWindowTest(SimpleTestCase):
    """The boundary the schedule page and every anchored fixture now share.

    Two fixtures in this suite used to place posts at `now + n days` and assert they appeared on the
    week the page renders. That held mid-week and failed on a Saturday and a Sunday, twice, for
    different reasons. Both now derive their dates from `schedule_week_start`, the view's own
    function — so this pins the one property that makes that safe, rather than trusting a run that
    happens to land on a convenient day.
    """

    def test_every_day_lands_inside_the_week_that_names_it(self):
        from datetime import timedelta
        from django.utils import timezone
        from .services import schedule_week_start
        today=timezone.localdate()
        for offset in range(-60, 61):
            day=today+timedelta(days=offset)
            start=schedule_week_start(day)
            self.assertLessEqual(start,day,f"{day} fell before its own week start")
            self.assertLess(day,start+timedelta(days=7),f"{day} fell outside its own week start")
            self.assertEqual(start.weekday(),0,f"{day} resolved to a {start.strftime('%A')}, not a Monday")

    def test_the_week_a_page_would_be_asked_for_contains_the_day(self):
        # `week_offset_for(day)` is what a fixture would pass as `?week=`. The property that matters:
        # the window the page then renders — this week's Monday plus that many weeks, seven days wide —
        # really does contain the day it was computed from.
        from datetime import timedelta
        from django.utils import timezone
        from .services import schedule_week_start, week_offset_for
        today=timezone.localdate()
        this_week=schedule_week_start(today)
        for offset in range(-30, 31):
            day=today+timedelta(days=offset)
            request=week_offset_for(day)
            start=this_week+timedelta(weeks=request)
            self.assertLessEqual(start,day,f"week {request} starts after {day}")
            self.assertLess(day,start+timedelta(days=7),f"{day} falls outside week {request}")


class PersonnelFileTest(TestCase):
    """REC-1, REC-2 and REC-3: what a personnel file is, where it goes, and who signed which text.

    Three claims hold this slice together. An export is a *read*, so it may carry nothing the reader
    could not open one at a time — and unlike a download it leaves the building, so it has to say what
    went into it. Archiving has to actually remove a record from the live file, because a timestamp
    that nothing reads is a disposition with no effect and recovery has nothing to recover. And a
    signature belongs to the version it was given for, which is why "outstanding on the 2026 handbook"
    and "never acknowledged any version of it" are different questions with different answers.
    """

    def setUp(self):
        import tempfile
        from datetime import timedelta
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from django.utils import timezone
        User=get_user_model()
        self.User=User
        self.timedelta=timedelta
        self.uploads=SimpleUploadedFile
        self.owner=User.objects.create_user(username="file-owner@example.com",password="correct horse battery staple")
        self.admin=User.objects.create_user(username="file-admin@example.com",password="correct horse battery staple")
        self.hr=User.objects.create_user(username="file-hr@example.com",password="correct horse battery staple")
        self.auditor=User.objects.create_user(username="file-auditor@example.com",password="correct horse battery staple")
        self.supervisor=User.objects.create_user(username="file-supervisor@example.com",password="correct horse battery staple")
        self.worker_user=User.objects.create_user(username="file-guard@example.com",password="correct horse battery staple")
        self.org=Organization.objects.create(legal_name="Dossier LLC",display_name="Dossier",slug="dossier-llc")
        for user,role in ((self.owner,Membership.Role.OWNER),(self.admin,Membership.Role.ADMIN),
                          (self.hr,Membership.Role.HR),(self.auditor,Membership.Role.AUDITOR),
                          (self.supervisor,Membership.Role.SUPERVISOR),(self.worker_user,Membership.Role.OFFICER)):
            Membership.objects.create(user=user,organization=self.org,role=role)
        self.north=Branch.objects.create(organization=self.org,name="North",city="Denton")
        self.south=Branch.objects.create(organization=self.org,name="South",city="Waco")
        self.person=Person.objects.create(organization=self.org,user=self.worker_user,branch=self.north,
            first_name="Ana",last_name="Delgado",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        self.colleague=Person.objects.create(organization=self.org,branch=self.south,
            first_name="Beth",last_name="Cole",status=Person.Status.ACTIVE,is_unarmed_officer=True)
        self.receipt=DocumentType.objects.create(organization=self.org,name="Direct deposit authorization",code="deposit")
        self.claim=DocumentType.objects.create(organization=self.org,name="Workers compensation claim",code="claim",
            sensitivity=DocumentType.Sensitivity.RESTRICTED)
        self.discipline=DocumentType.objects.create(organization=self.org,name="Investigation file",code="investigation",
            sensitivity=DocumentType.Sensitivity.SEALED)
        self.handbook=DocumentType.objects.create(organization=self.org,name="Employee handbook",code="handbook",
            audience=DocumentType.Audience.WORKFORCE,acknowledgment_required=True)
        self.bank_field=CustomFieldDefinition.objects.create(organization=self.org,name="TOPS receipt",key="tops",kind="text")
        self.secret_field=CustomFieldDefinition.objects.create(organization=self.org,name="Court order",key="court",
            kind="text",sensitive=True)
        PersonCustomValue.objects.create(organization=self.org,person=self.person,definition=self.bank_field,value="banked")
        PersonCustomValue.objects.create(organization=self.org,person=self.person,definition=self.secret_field,value="sealed-note")
        import uuid
        media=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        override=override_settings(MEDIA_ROOT=media.name); override.enable()
        self.addCleanup(override.disable); self.addCleanup(media.cleanup)
        self.file(self.receipt,self.person,"deposit.pdf")
        self.file(self.claim,self.person,"claim.pdf")
        self.file(self.discipline,self.person,"investigation.pdf")
        self.file(self.handbook,None,"handbook.pdf")
        # A personnel file whose owner has never worked a post has no time events to carry, and the
        # export would then look correct while proving nothing about its raw-evidence half.
        worked=timezone.now().replace(microsecond=0)-timedelta(days=3)
        Punch.objects.create(organization=self.org,person=self.person,client_event_id=uuid.uuid4(),
            kind=Punch.Kind.IN,occurred_at=worked)
        Punch.objects.create(organization=self.org,person=self.person,client_event_id=uuid.uuid4(),
            kind=Punch.Kind.OUT,occurred_at=worked+timedelta(hours=12))

    def file(self,document_type,person,name):
        return PersonDocument.objects.create(organization=self.org,person=person,document_type=document_type,
            file=self.uploads(name,b"%PDF-1.4\nbody",content_type="application/pdf"),original_name=name,
            content_type="application/pdf",size=13,sha256="0"*64,scan_status=PersonDocument.ScanStatus.CLEAN)

    def archive(self,document):
        """Run a real archive disposition end to end: request as owner, execute as a second approver."""
        self.client.force_login(self.owner)
        self.client.post(reverse("disposition_request",args=[document.pk]),
                         {"action":DispositionRequest.Action.ARCHIVE,"reason":"Retention date passed with no hold on it"})
        request=DispositionRequest.objects.get(document=document)
        self.client.force_login(self.admin)
        self.client.post(reverse("disposition_execute",args=[request.pk]))
        document.refresh_from_db()
        return request

    def export(self,as_user):
        self.client.force_login(as_user)
        return self.client.get(reverse("person_export",args=[self.person.pk]))

    # -- REC-1: what may leave the building --

    def test_an_export_carries_exactly_the_files_the_reader_could_open(self):
        from .services import personnel_file_bundle
        hr=personnel_file_bundle(self.org,self.person,Membership.Role.HR)
        auditor=personnel_file_bundle(self.org,self.person,Membership.Role.AUDITOR)
        self.assertEqual([row["record_type"] for row in hr["manifest"]],
                         ["Direct deposit authorization","Investigation file","Workers compensation claim"])
        self.assertEqual([row["record_type"] for row in auditor["manifest"]],["Direct deposit authorization"],
                         "rung one is the only thing an outside auditor may take with them")

    def test_the_zip_carries_the_dossier_manifest_and_the_permitted_files(self):
        import io, zipfile
        response=self.export(self.hr)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response["Content-Type"],"application/zip")
        names=sorted(zipfile.ZipFile(io.BytesIO(response.content)).namelist())
        self.assertIn("README.txt",names)
        self.assertIn("dossier.json",names)
        self.assertIn("records-manifest.csv",names)
        records=[name for name in names if name.startswith("records/")]
        self.assertEqual(len(records),3)
        self.assertTrue(all(name.startswith("records/") and ".." not in name for name in records))

    def test_a_worker_self_export_does_not_admit_the_file_about_them(self):
        import io, zipfile
        response=self.export(self.worker_user)
        self.assertEqual(response.status_code,200)
        archive=zipfile.ZipFile(io.BytesIO(response.content))
        manifest=archive.read("records-manifest.csv").decode()
        self.assertIn("Workers compensation claim",manifest)
        # The stronger half: silence, not annotation. "Investigation file — withheld because you may
        # not read it" would itself disclose that the company holds one on them, so the row is
        # absent from the manifest and from the README's withheld list alike.
        self.assertNotIn("Investigation file",manifest)
        self.assertNotIn("Investigation file",archive.read("README.txt").decode())
        self.assertNotIn("investigation"," ".join(archive.namelist()).lower())

    def test_a_zip_entry_name_is_built_by_the_code_and_not_taken_from_the_upload(self):
        from .services import personnel_file_bundle
        hostile=self.file(self.receipt,self.person,"../../etc/passwd.pdf")
        bundle=personnel_file_bundle(self.org,self.person,Membership.Role.HR)
        names=[name for name,_ in bundle["files"]]
        self.assertTrue(all(name.startswith("records/") for name in names))
        self.assertFalse(any(".." in name for name in names),f"escape attempt survived: {names}")
        self.assertTrue(all(name.endswith(".pdf") or ".pdf" in name for name in names))
        hostile.delete()

    def test_an_export_writes_an_audit_event_saying_what_left(self):
        self.export(self.auditor)
        event=AuditEvent.objects.get(action="person.exported",target_id=str(self.person.pk))
        self.assertEqual(event.metadata["records"],1)
        self.assertEqual(event.metadata["record_types"],["Direct deposit authorization"])
        self.assertEqual(event.metadata["files_included"],1)
        self.assertFalse(event.metadata["self_service"])

    def test_a_self_export_is_marked_as_one_in_the_audit_chain(self):
        self.export(self.worker_user)
        self.assertTrue(AuditEvent.objects.get(action="person.exported",target_id=str(self.person.pk)).metadata["self_service"])

    def test_only_a_record_reader_or_the_subject_may_export_a_file(self):
        # A supervisor is scope-capable but not a record reader, and an officer is only ever a
        # subject of their own file: these are the two doors the route has, and there is no third.
        # Note the corollary the view encodes — every role that *can* read records is company-level,
        # so the `permits_person` check is defence in depth rather than a live path today.
        self.assertEqual(self.export(self.supervisor).status_code,404,"a supervisor reads posts, not files")
        self.assertEqual(self.export(self.worker_user).status_code,200)
        other=Person.objects.create(organization=self.org,first_name="Cal",last_name="Diaz",status=Person.Status.ACTIVE)
        self.client.force_login(self.worker_user)
        self.assertEqual(self.client.get(reverse("person_export",args=[other.pk])).status_code,404,
                         "being somebody's colleague is not a licence to read their file")

    def test_an_audit_role_may_export_but_not_the_ordinary_supervisor_role(self):
        self.assertEqual(self.export(self.auditor).status_code,200)
        self.assertEqual(self.export(self.hr).status_code,200)

    def test_sensitive_custom_fields_follow_the_same_rule_as_the_person_screen(self):
        from .services import personnel_file_bundle
        manager=personnel_file_bundle(self.org,self.person,Membership.Role.HR,sensitive_fields=True)
        auditor=personnel_file_bundle(self.org,self.person,Membership.Role.AUDITOR,sensitive_fields=False)
        self.assertIn("sealed-note",[row["value"] for row in manager["dossier"]["custom_fields"]])
        self.assertNotIn("sealed-note",[row["value"] for row in auditor["dossier"]["custom_fields"]])
        self.assertIn("banked",[row["value"] for row in auditor["dossier"]["custom_fields"]])

    def test_wage_figures_are_not_copied_into_a_personnel_file(self):
        import json
        from .services import personnel_file_bundle
        bundle=personnel_file_bundle(self.org,self.person,Membership.Role.HR)
        rendered=json.dumps(bundle["dossier"],default=str)
        for column in ("estimated_pay","estimated_bill","overtime_hours","bill_rate"):
            self.assertNotIn(column,rendered,"two definitions of one number, one document apart")
        self.assertTrue(bundle["dossier"]["time_events"],"the raw events still belong here")
        self.assertIn("payroll export is the single source",bundle["readme"])

    def test_another_company_cannot_export_this_file(self):
        other_org=Organization.objects.create(legal_name="Elsewhere LLC",display_name="Elsewhere",slug="elsewhere-llc")
        outsider=self.User.objects.create_user(username="file-outsider@example.com",password="correct horse battery staple")
        Membership.objects.create(user=outsider,organization=other_org,role=Membership.Role.OWNER)
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse("person_export",args=[self.person.pk])).status_code,404)

    # -- REC-2: archive means something, and recovery exists --

    def test_an_archived_record_leaves_every_live_view_but_stays_reachable(self):
        document=PersonDocument.objects.get(original_name="claim.pdf")
        self.archive(document)
        self.client.force_login(self.hr)
        self.assertNotContains(self.client.get(reverse("documents")),"Workers compensation claim")
        self.assertNotContains(self.client.get(reverse("person_detail",args=[self.person.pk])+"?tab=documents"),
                               "Workers compensation claim")
        self.client.force_login(self.worker_user)
        self.assertNotContains(self.client.get(reverse("my_documents")),"Workers compensation claim")
        # Reachable, not gone: the shelf it was moved to, and the same view that can restore it.
        self.client.force_login(self.hr)
        self.assertContains(self.client.get(reverse("documents")+"?archived=1"),"Workers compensation claim")
        self.assertEqual(self.client.get(reverse("document_download",args=[document.pk])).status_code,200)

    def test_an_archived_record_stops_being_counted_as_live_evidence(self):
        from .scope import for_membership
        document=PersonDocument.objects.get(original_name="claim.pdf")
        membership=self.org.memberships.get(user=self.hr)
        before=self._records_section(membership)["total"]
        self.archive(document)
        self.assertEqual(self._records_section(membership)["total"],before-1,
                         "a queue that still counted an archived certificate would ask for it back")

    def _records_section(self,membership):
        from .scope import for_membership
        from .services import compliance_attendance
        return compliance_attendance(self.org,for_membership(membership),
            reader=(membership.role,None))["documents"]

    def test_the_personnel_tab_says_where_a_missing_record_went(self):
        self.archive(PersonDocument.objects.get(original_name="claim.pdf"))
        page=self.client.get(reverse("person_detail",args=[self.person.pk])+"?tab=documents")
        self.assertContains(page,"1 archived record")
        self.assertContains(page,"Retention review")

    def test_restoring_a_record_marks_the_disposition_as_reversed(self):
        document=PersonDocument.objects.get(original_name="claim.pdf")
        request=self.archive(document)
        self.client.force_login(self.owner)
        response=self.client.post(reverse("document_restore",args=[document.pk]),
            {"reason":"The claim was reopened and the file is needed again"})
        self.assertRedirects(response,reverse("retention_review"))
        document.refresh_from_db(); request.refresh_from_db()
        self.assertIsNone(document.archived_at)
        self.assertEqual(request.status,DispositionRequest.Status.RESTORED)
        self.assertEqual(request.restored_by_id,self.owner.id)
        self.assertIn("reopened",request.restore_reason)
        self.assertTrue(AuditEvent.objects.filter(action="document.restored",target_id=str(document.pk)).exists())
        self.client.force_login(self.hr)
        self.assertContains(self.client.get(reverse("documents")),"Workers compensation claim")

    def test_a_restore_needs_a_reason_worth_reading_later(self):
        document=PersonDocument.objects.get(original_name="claim.pdf")
        self.archive(document)
        self.client.force_login(self.owner)
        self.client.post(reverse("document_restore",args=[document.pk]),{"reason":"needed"})
        document.refresh_from_db()
        self.assertIsNotNone(document.archived_at,"a short reason must not silently reverse a decision")

    def test_a_deleted_file_refuses_to_be_restored_and_names_the_reason(self):
        from django.core.exceptions import ValidationError
        from .services import restore_disposition
        document=PersonDocument.objects.get(original_name="handbook.pdf")
        self.client.force_login(self.owner)
        self.client.post(reverse("disposition_request",args=[document.pk]),
            {"action":DispositionRequest.Action.DELETE,"reason":"Retention period ended and no hold applies to it"})
        request=DispositionRequest.objects.get(document=document)
        self.client.force_login(self.admin)
        self.client.post(reverse("disposition_execute",args=[request.pk]))
        document.refresh_from_db()
        # The in-memory `request` was loaded before the execute POST, so its status is still the
        # stale PENDING; passing it in would test the view's own guard rather than the real state.
        request.refresh_from_db()
        self.assertEqual(request.status,DispositionRequest.Status.EXECUTED)
        self.assertIsNotNone(document.deleted_at)
        with self.assertRaises(ValidationError) as caught:
            restore_disposition(request,self.owner,"We need the handbook back for an audit")
        self.assertIn("nothing to restore",str(caught.exception))
        self.assertEqual(document.file.name,"","the bytes really are gone, so the claim must not be soft")

    def test_an_archived_workforce_record_stops_generating_reminders(self):
        from django.core.exceptions import ValidationError
        from .services import queue_acknowledgment_reminders
        document=PersonDocument.objects.get(original_name="handbook.pdf")
        self.archive(document)
        with self.assertRaises(ValidationError) as caught:
            queue_acknowledgment_reminders(document,self.owner)
        self.assertIn("archived",str(caught.exception))

    def test_the_retention_page_separates_due_archived_and_gone(self):
        document=PersonDocument.objects.get(original_name="claim.pdf")
        self.archive(document)
        self.client.force_login(self.owner)
        page=self.client.get(reverse("retention_review"))
        self.assertContains(page,"<summary>Archived records &mdash; recoverable</summary>",html=True)
        self.assertContains(page,"<summary>Deleted records &mdash; not recoverable</summary>",html=True)
        self.assertContains(page,"Restore to the file")

    # -- REC-3: signatures across a chain of versions --

    def sign(self,user,document,signature="I agree"):
        self.client.force_login(user)
        # `confirm` is the acknowledgment itself — the form will not record a signature without the
        # box ticked, which is the point of having it.
        return self.client.post(reverse("document_acknowledge",args=[document.pk]),
                                {"confirm":"on","signature_name":signature})

    def two_versions(self,signed=None):
        """A handbook, an officer who signs it, and the revision filed afterwards.

        The order is the whole point: acknowledging a superseded text is refused by the acknowledge
        route, so a worker who signed the 2024 handbook signed it *before* the 2026 one existed.
        Signing after filing the second would silently test nothing.
        """
        first=PersonDocument.objects.get(original_name="handbook.pdf")
        if signed is not None:
            self.sign(signed,first)
        second=self.file(self.handbook,None,"handbook-2026.pdf")
        second.supersedes=first; second.save(update_fields=["supersedes"])
        return first,second

    def test_a_signature_on_an_older_version_is_not_counted_as_a_signature_on_the_new_one(self):
        from .services import signature_lineage
        first,second=self.two_versions(signed=self.worker_user)
        lineage=signature_lineage(second)
        self.assertEqual([person.pk for person in lineage["superseded_only"]],[self.person.pk])
        self.assertEqual(lineage["signed_current"],[],"the roster for the current text is not the history")

    def test_the_report_names_the_people_who_have_never_signed_any_version(self):
        from .services import signature_lineage
        first,second=self.two_versions(signed=self.worker_user)
        lineage=signature_lineage(second)
        self.assertEqual([person.full_name for person in lineage["never_signed"]],["Beth Cole"],
                         "this is the sentence an auditor asks for, and the old screen could not say it")

    def test_the_lineage_includes_versions_filed_after_the_row_being_viewed(self):
        from .services import signature_lineage
        first,second=self.two_versions()
        self.assertEqual([row.original_name for row in signature_lineage(first)["chain"]],
                         ["handbook.pdf","handbook-2026.pdf"])

    def test_the_two_questions_on_one_page_use_one_roster(self):
        from .services import outstanding_acknowledgments, signature_lineage
        first,second=self.two_versions(signed=self.worker_user)
        Person.objects.create(organization=self.org,first_name="Cal",last_name="Diaz",
            status=Person.Status.INACTIVE,is_unarmed_officer=True)
        lineage=signature_lineage(second)
        roster=self.org.people.exclude(status=Person.Status.INACTIVE)
        outstanding={person.pk for person in outstanding_acknowledgments(second,people=roster)}
        never={person.pk for person in lineage["never_signed"]}
        self.assertTrue(never <= outstanding,"a person cannot be never-signed and not outstanding")
        self.assertEqual({person.pk for person in lineage["roster"]},{self.person.pk,self.colleague.pk},
                         "an inactive worker belongs to neither list")

    def test_the_cross_revision_panel_only_appears_when_there_is_more_than_one_version(self):
        document=PersonDocument.objects.get(original_name="handbook.pdf")
        self.client.force_login(self.hr)
        self.assertNotContains(self.client.get(reverse("document_acknowledgments",args=[document.pk])),
                               "Across every version")
        self.two_versions()
        self.assertContains(self.client.get(reverse("document_acknowledgments",args=[document.pk])),
                            "Across every version")


class OnboardingTaskTest(TestCase):
    """ONB-1: a new hire's steps have an owner, a date, a state, and evidence behind the tick.

    The tests here aim at the invariants rather than the happy path, because the failure modes this
    slice can produce are silent ones: a checklist issued twice (so a step needs two completions), a
    deadline invented for somebody whose hire date was never recorded, a step marked complete while
    the record it names is missing or archived, a dashboard number that disagrees with the page it
    links to, and a sealed claim reported to a scheduler as "nothing filed".
    """

    def setUp(self):
        import tempfile
        from datetime import timedelta
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from django.utils import timezone
        User = get_user_model()
        self.timedelta = timedelta
        self.timezone = timezone
        self.today = timezone.localdate()
        self.owner = User.objects.create_user(username="ob-owner@example.com", password="pw-ob-owner")
        self.hr = User.objects.create_user(username="ob-hr@example.com", password="pw-ob-hr")
        self.auditor = User.objects.create_user(username="ob-auditor@example.com", password="pw-ob-auditor")
        self.worker = User.objects.create_user(username="ob-worker@example.com", password="pw-ob-worker")
        self.org = Organization.objects.create(legal_name="Ranger Security LLC", display_name="Ranger", slug="ranger-ob")
        for user, role in ((self.owner, Membership.Role.OWNER), (self.hr, Membership.Role.HR),
                           (self.auditor, Membership.Role.AUDITOR), (self.worker, Membership.Role.OFFICER)):
            Membership.objects.create(user=user, organization=self.org, role=role)
        self.hired = Person.objects.create(organization=self.org, first_name="Nadia", last_name="Orr",
                                          user=self.worker, hire_date=self.today - timedelta(days=10),
                                          status=Person.Status.ONBOARDING, is_unarmed_officer=True)
        # No hire date at all: the deadline cannot be derived, and inventing one from today would
        # make a data-entry gap somebody's lateness.
        self.unknown = Person.objects.create(organization=self.org, first_name="Sam", last_name="Iqbal",
                                             status=Person.Status.ONBOARDING, is_unarmed_officer=True)
        self.ack = DocumentType.objects.create(organization=self.org, name="Hand-order acknowledgment", code="hand-order")
        self.claim = DocumentType.objects.create(organization=self.org, name="Injury report", code="claim",
                                                 sensitivity=DocumentType.Sensitivity.SEALED)
        self.registration = CredentialType.objects.create(organization=self.org, name="Guard registration",
                                                         code="ob-guard-reg", applies_to=["unarmed"],
                                                         blocks_scheduling=True)
        self.step = OnboardingItem.objects.create(organization=self.org, name="Read the post orders",
                                                  code="post-orders", kind=OnboardingItem.Kind.TASK,
                                                  due_within_days=5, owner=OnboardingItem.Owner.PERSON)
        self.doc_step = OnboardingItem.objects.create(organization=self.org, name="Hand-order on file",
                                                      code="hand-order", kind=OnboardingItem.Kind.DOCUMENT,
                                                      document_type=self.ack, due_within_days=5)
        self.cred_step = OnboardingItem.objects.create(organization=self.org, name="Registration on file",
                                                       code="registration", kind=OnboardingItem.Kind.CREDENTIAL,
                                                       credential_type=self.registration, due_within_days=10)
        self.armed_only = OnboardingItem.objects.create(organization=self.org, name="Commission card",
                                                        code="commission", kind=OnboardingItem.Kind.TASK,
                                                        applies_to=["commissioned"], due_within_days=1)
        media = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(media.cleanup)
        self.uploads = SimpleUploadedFile

    def file(self, kind, person, name):
        return PersonDocument.objects.create(
            organization=self.org, person=person, document_type=kind,
            file=self.uploads(name, b"%PDF-1.4\nbody", content_type="application/pdf"),
            original_name=name, content_type="application/pdf", size=13, sha256="0" * 64,
            scan_status=PersonDocument.ScanStatus.CLEAN)

    def board(self, person, as_user=None):
        from .scope import for_membership
        from .services import onboarding_board
        role = Membership.Role.OWNER
        subject = None
        if as_user is not None:
            membership = self.org.memberships.get(user=as_user)
            role = membership.role
            subject = self.org.people.filter(user=as_user).values_list("id", flat=True).first()
            person_scope = for_membership(membership)
            return onboarding_board(self.org, person, reader=(role, subject)), person_scope
        return onboarding_board(self.org, person), None

    # -- issuing --

    def test_issuing_a_checklist_twice_creates_no_duplicate_step(self):
        from .services import provision_onboarding_tasks
        first = provision_onboarding_tasks(self.hired)
        second = provision_onboarding_tasks(self.hired)
        self.assertEqual(len(first), 3, "Three steps apply to an unarmed officer: task, hand-order, registration")
        self.assertEqual(second, [], "Re-issuing must create nothing, or a step needs ticking twice")
        self.assertEqual(OnboardingTask.objects.filter(person=self.hired).count(), 3)

    def test_a_step_bound_to_a_category_is_not_issued_outside_it(self):
        from .services import provision_onboarding_tasks
        created = provision_onboarding_tasks(self.hired)
        self.assertNotIn("Commission card", [task.item.name for task in created])
        self.armed_only.refresh_from_db()
        self.hired.is_commissioned_officer = True
        self.hired.save()
        added = provision_onboarding_tasks(self.hired)
        self.assertIn("Commission card", [task.item.name for task in added])

    def test_a_hire_date_absent_from_the_profile_yields_no_invented_deadline(self):
        from .services import provision_onboarding_tasks
        tasks = provision_onboarding_tasks(self.unknown)
        self.assertTrue(tasks, "Steps are still owed; only the date is unknowable")
        self.assertIsNone(tasks[0].due_on)
        board, _ = self.board(self.unknown)
        self.assertEqual(board["overdue"], [], "Nothing can be late against a date nobody recorded")
        self.assertTrue(all(row["no_hire_date"] for row in board["rows"]))

    def test_the_deadline_is_the_hire_date_plus_the_steps_lead_time(self):
        from .services import provision_onboarding_tasks
        task = [item for item in provision_onboarding_tasks(self.hired) if item.item == self.step][0]
        self.assertEqual(task.due_on, self.hired.hire_date + self.timedelta(days=5))
        self.step.due_within_days = 30
        self.step.save()
        task.refresh_from_db()
        self.assertEqual(task.due_on, self.hired.hire_date + self.timedelta(days=5),
                         "Editing the rule must not rewrite a date a reminder was already sent against")

    # -- evidence behind the tick --

    def test_a_step_that_names_a_record_refuses_to_be_marked_complete_without_it(self):
        from .services import provision_onboarding_tasks
        task = [item for item in provision_onboarding_tasks(self.hired) if item.item == self.doc_step][0]
        self.client.force_login(self.hr)
        response = self.client.post(reverse("onboarding_task_decide", args=[task.pk]),
                                    {"action": "complete", "note": ""}, follow=True)
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, OnboardingTask.Status.OPEN)
        self.assertTrue(any("cannot be marked complete" in item
                            for item in [str(item) for item in response.context["messages"]]),
                        [str(item) for item in response.context["messages"]])

    def test_filing_the_record_makes_the_step_completable_and_who_decided_is_kept(self):
        from .services import provision_onboarding_tasks
        task = [item for item in provision_onboarding_tasks(self.hired) if item.item == self.doc_step][0]
        self.file(self.ack, self.hired, "hand-order.pdf")
        board, _ = self.board(self.hired)
        row = [row for row in board["rows"] if row["task"].pk == task.pk][0]
        self.assertTrue(row["evidence"]["satisfied"])
        self.client.force_login(self.hr)
        self.client.post(reverse("onboarding_task_decide", args=[task.pk]), {"action": "complete"})
        task.refresh_from_db()
        self.assertEqual(task.status, OnboardingTask.Status.DONE)
        self.assertEqual(task.decided_by, self.hr)
        self.assertTrue(AuditEvent.objects.filter(action="onboarding.decided", target_id=str(task.pk)).exists())

    def test_an_archived_record_does_not_satisfy_the_step_that_named_it(self):
        from .services import provision_onboarding_tasks
        document = self.file(self.ack, self.hired, "hand-order.pdf")
        document.archived_at = self.timezone.now()
        document.save(update_fields=["archived_at"])
        task = [item for item in provision_onboarding_tasks(self.hired) if item.item == self.doc_step][0]
        board, _ = self.board(self.hired)
        row = [row for row in board["rows"] if row["task"].pk == task.pk][0]
        self.assertFalse(row["evidence"]["satisfied"],
                         "A record archived under a retention decision is out of the file; a step "
                         "still marked complete would be reporting the file from before the decision")

    def test_a_lapsed_credential_does_not_satisfy_the_step_that_named_it(self):
        from .services import provision_onboarding_tasks
        Credential.objects.create(organization=self.org, person=self.hired, credential_type=self.registration,
                                  status=Credential.Status.ACTIVE, number="G123",
                                  expires_on=self.today - self.timedelta(days=2))
        task = [item for item in provision_onboarding_tasks(self.hired) if item.item == self.cred_step][0]
        board, _ = self.board(self.hired)
        row = [row for row in board["rows"] if row["task"].pk == task.pk][0]
        self.assertFalse(row["evidence"]["satisfied"])
        self.assertEqual(row["evidence"]["state"], "expired")

    def test_a_sealed_record_is_reported_as_hidden_and_never_as_missing(self):
        from .services import provision_onboarding_tasks
        sealed_step = OnboardingItem.objects.create(organization=self.org, name="Injury report on file",
                                                   code="claim-step", kind=OnboardingItem.Kind.DOCUMENT,
                                                   document_type=self.claim, due_within_days=5)
        self.file(self.claim, self.hired, "claim.pdf")
        task = provision_onboarding_tasks(self.hired, items=[sealed_step])[0]
        board, _ = self.board(self.hired, as_user=self.auditor)
        row = [row for row in board["rows"] if row["task"].pk == task.pk][0]
        self.assertIsNone(row["evidence"]["satisfied"],
                          "Counting a hidden record as absent would answer 'nothing filed' about a "
                          "claim that is filed and sealed from this reader")
        self.assertEqual(row["evidence"]["state"], "hidden")
        # And the step itself is still named: owing a record is not the secret; holding it is.
        self.assertEqual(row["item"].name, "Injury report on file")

    def test_the_subject_of_a_sealed_record_is_not_a_reader_of_it(self):
        from .services import provision_onboarding_tasks
        sealed_step = OnboardingItem.objects.create(organization=self.org, name="Injury report on file",
                                                   code="claim-step", kind=OnboardingItem.Kind.DOCUMENT,
                                                   document_type=self.claim, due_within_days=5)
        self.file(self.claim, self.hired, "claim.pdf")
        task = provision_onboarding_tasks(self.hired, items=[sealed_step])[0]
        board, _ = self.board(self.hired, as_user=self.worker)
        row = [row for row in board["rows"] if row["task"].pk == task.pk][0]
        self.assertEqual(row["evidence"]["state"], "hidden",
                         "Being the officer the record is about does not open it: the sealed rung "
                         "denies the subject even a role that may read sealed files")

    # -- who may decide --

    def test_waiving_a_step_requires_a_reason_that_survives_the_argument(self):
        from .services import provision_onboarding_tasks
        task = [item for item in provision_onboarding_tasks(self.hired) if item.item == self.doc_step][0]
        self.client.force_login(self.hr)
        self.client.post(reverse("onboarding_task_decide", args=[task.pk]), {"action": "waive", "note": "n/a"})
        task.refresh_from_db()
        self.assertEqual(task.status, OnboardingTask.Status.OPEN)
        self.client.post(reverse("onboarding_task_decide", args=[task.pk]),
                         {"action": "waive", "note": "Armed post only; this officer is unarmed"})
        task.refresh_from_db()
        self.assertEqual(task.status, OnboardingTask.Status.WAIVED)
        self.assertIn("unarmed", task.note.lower(), "The reason is stored, not the fact that a box was ticked")

    def test_the_officer_a_step_is_about_cannot_waive_it(self):
        from .services import provision_onboarding_tasks
        task = [item for item in provision_onboarding_tasks(self.hired) if item.item == self.doc_step][0]
        self.client.force_login(self.worker)
        self.client.post(reverse("onboarding_task_decide", args=[task.pk]),
                         {"action": "waive", "note": "Armed post only; this officer is unarmed"})
        task.refresh_from_db()
        self.assertEqual(task.status, OnboardingTask.Status.OPEN,
                         "A waiver decides that an insurance or licensing condition does not apply, "
                         "and the person it is about is the last party who should be able to remove it")

    def test_an_officer_may_close_their_own_step_but_not_the_offices_one(self):
        from .services import provision_onboarding_tasks
        staff_step = OnboardingItem.objects.create(organization=self.org, name="Enter the rate into payroll",
                                                  code="rate-setup", kind=OnboardingItem.Kind.TASK,
                                                  owner=OnboardingItem.Owner.STAFF, due_within_days=2)
        provision_onboarding_tasks(self.hired, items=[self.step, staff_step])
        own = OnboardingTask.objects.get(person=self.hired, item=self.step)
        office = OnboardingTask.objects.get(person=self.hired, item=staff_step)
        self.client.force_login(self.worker)
        self.client.post(reverse("onboarding_task_decide", args=[own.pk]), {"action": "complete"})
        own.refresh_from_db()
        self.assertEqual(own.status, OnboardingTask.Status.DONE)
        self.client.post(reverse("onboarding_task_decide", args=[office.pk]), {"action": "complete"})
        office.refresh_from_db()
        self.assertEqual(office.status, OnboardingTask.Status.OPEN,
                         "The office owns its steps; an officer ticking them reports work done that "
                         "was never theirs to do")

    # -- one definition of the numbers --

    def test_the_aggregate_counts_equal_the_checklist_rows(self):
        from .services import onboarding_progress, provision_onboarding_tasks
        for person in (self.hired, self.unknown):
            provision_onboarding_tasks(person)
        Credential.objects.create(organization=self.org, person=self.hired, credential_type=self.registration,
                                  status=Credential.Status.ACTIVE, expires_on=self.today + self.timedelta(days=300))
        from .scope import ActorScope
        progress = onboarding_progress(self.org, ActorScope(None))
        for person in (self.hired, self.unknown):
            board, _ = self.board(person)
            self.assertEqual(progress[person.pk]["open"], len(board["outstanding"]),
                             "The tile and the page must be the same calculation")
            self.assertEqual(progress[person.pk]["overdue"], len(board["overdue"]))

    def test_a_late_step_is_chased_once_per_date_and_never_by_sms(self):
        from .services import provision_onboarding_tasks, queue_onboarding_reminders
        provision_onboarding_tasks(self.hired)
        OnboardingTask.objects.filter(person=self.hired, item=self.step).update(
            due_on=self.today - self.timedelta(days=2))
        first = queue_onboarding_reminders()
        second = queue_onboarding_reminders()
        self.assertGreater(first, 0)
        self.assertEqual(second, 0, "A reminder pass runs every minute; the late step must not be "
                                   "announced every minute")
        notices = Notification.objects.filter(event_type="onboarding.overdue")
        self.assertTrue(notices)
        for notice in notices:
            self.assertTrue(notice.deduplication_key, "A blank key disables the tenant uniqueness guard")
            self.assertNotEqual(notice.channel, Notification.Channel.SMS, "NTF-4 consent is unmet")
        self.assertIn(self.worker.pk, [notice.recipient_id for notice in notices])

    def test_a_step_the_office_owes_is_chased_to_the_office_not_the_officer(self):
        from .services import provision_onboarding_tasks, queue_onboarding_reminders
        staff_step = OnboardingItem.objects.create(organization=self.org, name="Enter the rate into payroll",
                                                  code="rate-setup", kind=OnboardingItem.Kind.TASK,
                                                  owner=OnboardingItem.Owner.STAFF,
                                                  due_within_days=1)
        provision_onboarding_tasks(self.hired, items=[staff_step])
        OnboardingTask.objects.filter(item=staff_step).update(due_on=self.today - self.timedelta(days=1))
        queue_onboarding_reminders()
        recipients = set(Notification.objects.filter(event_type="onboarding.overdue").values_list("recipient_id", flat=True))
        self.assertNotIn(self.worker.pk, recipients)
        self.assertIn(self.hr.pk, recipients)

    def test_the_worker_command_issues_to_nobody_twice_and_chases_once(self):
        from django.core.management import call_command
        call_command("onboarding_reminders")
        count = OnboardingTask.objects.count()
        self.assertGreater(count, 0)
        call_command("onboarding_reminders")
        self.assertEqual(OnboardingTask.objects.count(), count)

    def test_the_checklist_page_is_an_office_page(self):
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse("onboarding_settings")).status_code, 403)
        self.client.force_login(self.hr)
        self.assertEqual(self.client.get(reverse("onboarding_settings")).status_code, 200)

    def test_the_officers_own_page_lists_their_steps_without_an_office_visit(self):
        from .services import provision_onboarding_tasks
        provision_onboarding_tasks(self.hired)
        self.client.force_login(self.worker)
        response = self.client.get(reverse("my_onboarding"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Read the post orders")
        self.assertContains(response, "past date")

    def test_a_person_from_another_company_cannot_reach_the_checklist(self):
        from .services import provision_onboarding_tasks
        task = provision_onboarding_tasks(self.hired)[0]
        other = Organization.objects.create(legal_name="Other LLC", display_name="Other", slug="other-ob")
        other_owner = get_user_model().objects.create_user(username="other-ob@example.com", password="pw")
        Membership.objects.create(user=other_owner, organization=other, role=Membership.Role.OWNER)
        self.client.force_login(other_owner)
        # No personnel record behind that login, so there is no checklist to show and the page says
        # so and moves on rather than rendering somebody else's steps.
        self.assertEqual(self.client.get(reverse("my_onboarding")).status_code, 302)
        self.assertEqual(self.client.post(reverse("onboarding_task_decide", args=[task.pk]),
                                          {"action": "complete"}).status_code, 404)


class RegistryCheckTest(TestCase):
    """CMP-3: a dated, attributed registry look-up that ages — and does not move the rate when it
    has merely gone unexamined."""

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User = get_user_model()
        self.timedelta = timedelta
        self.today = timezone.localdate()
        self.owner = User.objects.create_user(username="reg-owner@example.com", password="pw-reg-owner")
        self.hr = User.objects.create_user(username="reg-hr@example.com", password="pw-reg-hr")
        self.worker = User.objects.create_user(username="reg-worker@example.com", password="pw-reg-worker")
        self.org = Organization.objects.create(legal_name="Post LLC", display_name="Post", slug="post-reg")
        for user, role in ((self.owner, Membership.Role.OWNER), (self.hr, Membership.Role.HR),
                           (self.worker, Membership.Role.OFFICER)):
            Membership.objects.create(user=user, organization=self.org, role=role)
        self.person = Person.objects.create(organization=self.org, first_name="Iris", last_name="Kant",
                                           user=self.worker, is_unarmed_officer=True,
                                           status=Person.Status.ACTIVE)
        self.kind = CredentialType.objects.create(organization=self.org, name="Guard registration",
                                                 code="reg-guard", applies_to=["unarmed"],
                                                 registry_check_within_days=90, blocks_scheduling=True,
                                                 jurisdiction="Texas",
                                                 authority_url="https://statutes.capitol.texas.gov/Docs/OC/htm/OC.1702.htm",
                                                 authority_reference="§1702.302",
                                                 interpretation="Every unarmed officer holds a current registration.",
                                                 effective_from=self.today - self.timedelta(days=100),
                                                 approved_by=self.owner, approved_at=self.today)
        # ACTIVE, not UNVERIFIED: an unverified credential is already compliance attention for its
        # own reason, and a test built on it cannot tell a registry lapse apart from that. The
        # promotion path is tested separately, with the status it actually starts from.
        self.credential = Credential.objects.create(organization=self.org, person=self.person,
                                                   credential_type=self.kind, number="G-9",
                                                   status=Credential.Status.ACTIVE,
                                                   expires_on=self.today + self.timedelta(days=200))

    def attendance(self):
        from .scope import ActorScope
        from .services import compliance_attendance, compliance_summary
        kinds = compliance_attendance(self.org, ActorScope(None), reader=(Membership.Role.OWNER, None))
        return kinds, compliance_summary(kinds)

    def test_no_check_at_all_is_a_lapse_and_does_not_move_the_rate(self):
        kinds, summary = self.attendance()
        before = summary["rate"]
        row = [item for item in kinds["credentials"]["rows"] if item["person"] == self.person][0]
        self.assertFalse(row["_needs"], "An unverified credential is already attention for its own "
                                        "reason; the missing check must not add a second cause")
        self.assertTrue(row["registry"]["lapsed"])
        self.assertEqual(before, summary["rate"])

    def test_a_check_that_came_back_expired_is_attention(self):
        CredentialRegistryCheck.objects.create(organization=self.org, credential=self.credential,
                                               checked_on=self.today - self.timedelta(days=3),
                                               result=CredentialRegistryCheck.Result.EXPIRED,
                                               checked_by=self.hr)
        self.credential.status = Credential.Status.ACTIVE
        self.credential.save(update_fields=["status"])
        kinds, summary = self.attendance()
        row = [item for item in kinds["credentials"]["rows"] if item["person"] == self.person][0]
        self.assertTrue(row["registry"]["adverse"])
        self.assertTrue(row["_needs"], "We looked, and the registry says the number is not valid")

    def test_a_current_clean_check_neither_lapses_nor_demands_anything(self):
        CredentialRegistryCheck.objects.create(organization=self.org, credential=self.credential,
                                               checked_on=self.today - self.timedelta(days=10),
                                               result=CredentialRegistryCheck.Result.VALID,
                                               checked_by=self.hr)
        kinds, _ = self.attendance()
        row = [item for item in kinds["credentials"]["rows"] if item["person"] == self.person][0]
        self.assertFalse(row["registry"]["lapsed"])
        self.assertFalse(row["registry"]["adverse"])

    def test_the_stale_check_moves_the_number_but_the_lapse_never_does(self):
        _, before = self.attendance()
        CredentialRegistryCheck.objects.create(organization=self.org, credential=self.credential,
                                               checked_on=self.today - self.timedelta(days=400),
                                               result=CredentialRegistryCheck.Result.VALID, checked_by=self.hr)
        _, after_stale = self.attendance()
        self.assertEqual(before["attention"], after_stale["attention"],
                         "A check nobody has repeated is our filing gap, not the officer's status")
        CredentialRegistryCheck.objects.create(organization=self.org, credential=self.credential,
                                               checked_on=self.today - self.timedelta(days=1),
                                               result=CredentialRegistryCheck.Result.NOT_FOUND, checked_by=self.hr)
        _, after_adverse = self.attendance()
        self.assertGreater(after_adverse["attention"], after_stale["attention"])

    def test_a_clean_check_writes_the_verification_column_that_had_never_been_written(self):
        from .services import record_registry_check
        self.credential.status = Credential.Status.UNVERIFIED
        self.credential.verified_at = None
        self.credential.save(update_fields=["status", "verified_at"])
        record_registry_check(self.credential, self.hr, self.today, CredentialRegistryCheck.Result.VALID)
        self.credential.refresh_from_db()
        self.assertIsNotNone(self.credential.verified_at)
        self.assertEqual(self.credential.status, Credential.Status.ACTIVE)

    def test_a_clean_check_does_not_overrule_a_status_the_office_set(self):
        from .services import record_registry_check
        self.credential.status = Credential.Status.SUSPENDED
        self.credential.save(update_fields=["status"])
        record_registry_check(self.credential, self.hr, self.today, CredentialRegistryCheck.Result.VALID)
        self.credential.refresh_from_db()
        self.assertEqual(self.credential.status, Credential.Status.SUSPENDED,
                         "The registry agreeing with nothing is not a reason to lift a suspension")

    def test_checks_are_history_and_the_newest_one_answers_the_question(self):
        from .services import registry_checks_by_credential
        CredentialRegistryCheck.objects.create(organization=self.org, credential=self.credential,
                                               checked_on=self.today - self.timedelta(days=200),
                                               result=CredentialRegistryCheck.Result.VALID, checked_by=self.hr)
        recent = CredentialRegistryCheck.objects.create(organization=self.org, credential=self.credential,
                                                       checked_on=self.today,
                                                       result=CredentialRegistryCheck.Result.ATTENTION,
                                                       checked_by=self.hr)
        latest = registry_checks_by_credential(self.org, [self.credential.pk])
        self.assertEqual(latest[self.credential.pk].pk, recent.pk)
        self.assertEqual(CredentialRegistryCheck.objects.filter(credential=self.credential).count(), 2)

    def test_an_adverse_result_is_told_to_the_office_and_the_officer_but_not_to_its_author(self):
        self.client.force_login(self.hr)
        self.client.post(reverse("credential_registry_check", args=[self.credential.pk]),
                         {"result": "not_found", "checked_on": self.today.isoformat()})
        recipients = set(Notification.objects.filter(event_type="credential.registry_adverse")
                         .values_list("recipient_id", flat=True))
        self.assertIn(self.worker.pk, recipients)
        self.assertNotIn(self.hr.pk, recipients)

    def test_a_malformed_date_is_answered_with_a_message_not_an_error_page(self):
        self.client.force_login(self.hr)
        response = self.client.post(reverse("credential_registry_check", args=[self.credential.pk]),
                                    {"result": "valid", "checked_on": "yesterday-ish"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CredentialRegistryCheck.objects.count(), 0)

    def test_an_unknown_result_is_refused(self):
        self.client.force_login(self.hr)
        self.client.post(reverse("credential_registry_check", args=[self.credential.pk]),
                         {"result": "looks fine", "checked_on": self.today.isoformat()})
        self.assertEqual(CredentialRegistryCheck.objects.count(), 0)

    def test_recording_a_check_is_an_offices_act_not_the_officers(self):
        self.client.force_login(self.worker)
        response = self.client.post(reverse("credential_registry_check", args=[self.credential.pk]),
                                    {"result": "valid", "checked_on": self.today.isoformat()})
        self.assertEqual(response.status_code, 403)

    def test_a_check_for_another_companys_credential_is_not_found(self):
        other = Organization.objects.create(legal_name="Far LLC", display_name="Far", slug="far-reg")
        other_owner = get_user_model().objects.create_user(username="far-reg@example.com", password="pw")
        Membership.objects.create(user=other_owner, organization=other, role=Membership.Role.OWNER)
        self.client.force_login(other_owner)
        response = self.client.post(reverse("credential_registry_check", args=[self.credential.pk]),
                                    {"result": "valid", "checked_on": self.today.isoformat()})
        self.assertEqual(response.status_code, 404)


class CoverageGapTest(TestCase):
    """SCH-4: does removing this officer leave the *post* uncovered — measured by the same rule the
    coverage report uses, before the row disappears."""

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User = get_user_model()
        self.timedelta = timedelta
        self.now = timezone.now().replace(microsecond=0)
        self.owner = User.objects.create_user(username="gap-owner@example.com", password="pw-gap-owner")
        self.org = Organization.objects.create(legal_name="Gate LLC", display_name="Gate", slug="gate-gap")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.Role.OWNER)
        self.client_user = Client.objects.create(organization=self.org, name="Hillcrest")
        self.site = Site.objects.create(organization=self.org, client=self.client_user, name="Gate house",
                                        address="1 Gate Rd")
        self.ana = Person.objects.create(organization=self.org, first_name="Ana", last_name="Del", is_unarmed_officer=True)
        self.bo = Person.objects.create(organization=self.org, first_name="Bo", last_name="Eno", is_unarmed_officer=True)
        # A second registered officer: relief has to be somebody *else* standing the same hours, and
        # a fixture that puts the same officer on two overlapping posts measures the double-booking
        # rule instead of coverage.
        self.carla = Person.objects.create(organization=self.org, first_name="Carla", last_name="Dune", is_unarmed_officer=True)
        self.registration = CredentialType.objects.create(organization=self.org, name="Registration",
                                                          code="gap-reg", applies_to=["unarmed"],
                                                          blocks_scheduling=True, jurisdiction="Texas",
                                                          authority_url="https://statutes.capitol.texas.gov/Docs/OC/htm/OC.1702.htm",
                                                          authority_reference="§1702.302",
                                                          interpretation="An officer stands a post only while the registration is current.",
                                                          effective_from=self.now.date() - timedelta(days=100),
                                                          approved_by=self.owner, approved_at=self.now)
        self.site.required_credentials.add(self.registration)
        for officer in (self.ana, self.carla):
            Credential.objects.create(organization=self.org, person=officer, credential_type=self.registration,
                                      status=Credential.Status.ACTIVE,
                                      expires_on=self.now.date() + timedelta(days=300))

    def post(self, officer, start_offset_hours, hours, status=Shift.Status.PUBLISHED):
        starts = self.now + self.timedelta(hours=start_offset_hours)
        return Shift.objects.create(organization=self.org, site=self.site, officer=officer,
                                    starts_at=starts, ends_at=starts + self.timedelta(hours=hours),
                                    status=status, post_name="Night gate")

    def gaps(self, shift):
        from .scope import ActorScope
        from .services import uncovered_windows
        return uncovered_windows(self.org, ActorScope(None), shift)

    def test_the_report_and_the_gap_check_use_one_definition_of_covered(self):
        from .scope import ActorScope
        from .services import coverage_report, coverage_state
        only = self.post(self.bo, 8, 8)       # bo has no registration → at risk
        filled = self.post(self.ana, 24, 8)   # ana is registered
        report = coverage_report(self.org, ActorScope(None), self.now, self.now + self.timedelta(days=3))
        self.assertIn(only.pk, [item["shift"].pk for item in report["at_risk"]])
        self.assertIn(filled.pk, [item.pk for item in report["filled"]])
        # The same predicate, applied by hand to every post in the window, must sort them the same way.
        counted = {"filled": [], "at_risk": [], "unfilled": []}
        for shift in self.org.shifts.exclude(status__in=[Shift.Status.CANCELLED, Shift.Status.DRAFT]):
            state, _ = coverage_state(shift)
            counted[state].append(shift.pk)
        self.assertEqual(set(counted["filled"]), {item.pk for item in report["filled"]},
                         "Two implementations of 'does this post cover ground' is how the advisory and "
                         "the report start disagreeing about the same night")
        self.assertEqual(set(counted["at_risk"]), {item["shift"].pk for item in report["at_risk"]})

    def test_cancelling_the_only_tour_reports_the_hours_it_removes(self):
        shift = self.post(self.ana, 8, 8)
        gaps = self.gaps(shift)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["hours"], 8.0)

    def test_a_relief_officer_standing_the_same_hours_closes_the_gap(self):
        shift = self.post(self.ana, 8, 8)
        self.post(self.carla, 8, 8)
        self.assertEqual(self.gaps(shift), [])

    def test_partial_overlap_leaves_only_the_uncovered_remainder(self):
        shift = self.post(self.ana, 8, 8)
        self.post(self.carla, 8, 3)
        gaps = self.gaps(shift)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["hours"], 5.0)

    def test_an_officer_who_may_not_work_there_is_not_coverage(self):
        # The Texas case: the neighbour's post is staffed, but by someone whose registration is not
        # on file, so counting it as covered would hide the hole the schedule actually has.
        shift = self.post(self.ana, 8, 8)
        self.post(self.bo, 8, 8)
        gaps = self.gaps(shift)
        self.assertEqual(gaps[0]["hours"], 8.0, "An at-risk officer covers nothing")

    def test_a_draft_never_closes_a_gap(self):
        shift = self.post(self.ana, 8, 8)
        self.post(self.carla, 8, 8, status=Shift.Status.DRAFT)
        self.assertEqual(self.gaps(shift)[0]["hours"], 8.0)

    def test_the_cancel_route_records_the_hours_it_predicted(self):
        shift = self.post(self.ana, 8, 8)
        predicted = self.gaps(shift)[0]["hours"]
        self.client.force_login(self.owner)
        response = self.client.post(reverse("shift_cancel", args=[shift.pk]),
                                    {"reason": "Officer called in sick and no relief was found"}, follow=True)
        self.assertEqual(response.status_code, 200)
        event = AuditEvent.objects.get(action="shift.cancelled", target_id=str(shift.pk))
        self.assertEqual(event.metadata["uncovered_hours"], predicted)
        messages = [str(item) for item in response.context["messages"]]
        self.assertTrue(any("uncovered for 8.0h" in item for item in messages), messages)

    def test_cancelling_removes_the_post_from_the_report_that_could_otherwise_hide_the_hole(self):
        from .scope import ActorScope
        from .services import coverage_report
        shift = self.post(self.ana, 8, 8)
        before = coverage_report(self.org, ActorScope(None), self.now, self.now + self.timedelta(days=1))
        self.client.force_login(self.owner)
        self.client.post(reverse("shift_cancel", args=[shift.pk]), {"reason": "No relief was found"})
        after = coverage_report(self.org, ActorScope(None), self.now, self.now + self.timedelta(days=1))
        self.assertEqual(after["total"], before["total"] - 1)
        self.assertEqual(after["unfilled"], [], "The per-post view shows no unfilled row, because the "
                                                "post itself is gone — which is exactly why the hours "
                                                "have to be measured before the cancel, not after")
        self.assertEqual(self.gaps(Shift.objects.get(pk=shift.pk))[0]["hours"], 8.0)

    def test_declining_a_claim_states_the_hours_that_stay_uncovered(self):
        shift = self.post(None, 8, 6)
        claim = ShiftClaim.objects.create(organization=self.org, shift=shift, officer=self.ana)
        self.client.force_login(self.owner)
        response = self.client.post(reverse("shift_claim_decide", args=[claim.pk]),
                                    {"action": "rejected"}, follow=True)
        self.assertTrue(any("uncovered for 6.0h" in item
                            for item in [str(item) for item in response.context["messages"]]),
                        [str(item) for item in response.context["messages"]])

    def test_declining_a_claim_for_a_post_another_officer_already_covers_is_quiet(self):
        shift = self.post(None, 8, 6)
        self.post(self.carla, 8, 6)
        claim = ShiftClaim.objects.create(organization=self.org, shift=shift, officer=self.bo)
        self.client.force_login(self.owner)
        response = self.client.post(reverse("shift_claim_decide", args=[claim.pk]),
                                    {"action": "rejected"}, follow=True)
        messages = [str(item) for item in response.context["messages"]]
        self.assertTrue(all("uncovered" not in item for item in messages), messages)


class PayCategoryTest(TestCase):
    """PAY-2 and PAY-5: categories a human had to assert, priced by rules that can change.

    The arithmetic is what these tests aim at, because each category shape has a different failure
    mode that all look correct on screen: an unpaid break that is deducted from the *line* but not
    from the week (so overtime is still charged for hours nobody was paid for), a premium that
    prices the whole hour again, a carve-out whose hours vanish, and a figure that quietly re-prices
    itself when somebody edits a multiplier after the period closed.
    """

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        from .services import ensure_pay_categories
        User = get_user_model()
        self.timedelta = timedelta
        # Anchored to *local* time, not `timezone.now()`: every window these fixtures build is derived
        # from `self.now.date()`, and the product's week math runs on `localdate()`. After 19:00 in a
        # UTC-5 deployment the two dates differ, `now().date()` flips to Monday while the schedule page
        # is still showing Sunday's week, and the fixture's "Tuesday" post lands a week away from the
        # window it is asserted to appear in.
        self.now = timezone.localtime().replace(microsecond=0, second=0)
        self.owner = User.objects.create_user(username="pc-owner@example.com", password="pw-pc-owner")
        self.clerk = User.objects.create_user(username="pc-payroll@example.com", password="pw-pc-payroll")
        self.worker = User.objects.create_user(username="pc-worker@example.com", password="pw-pc-worker")
        self.org = Organization.objects.create(legal_name="Ledger Gate LLC", display_name="Ledger", slug="ledger-pc")
        for user, role in ((self.owner, Membership.Role.OWNER), (self.clerk, Membership.Role.PAYROLL),
                           (self.worker, Membership.Role.OFFICER)):
            Membership.objects.create(user=user, organization=self.org, role=role)
        self.client_user = Client.objects.create(organization=self.org, name="Hillcrest")
        self.site = Site.objects.create(organization=self.org, client=self.client_user, name="Gate", address="1 Gate")
        self.ana = Person.objects.create(organization=self.org, first_name="Ana", last_name="Del",
                                        user=self.worker, hourly_rate=Decimal("20.00"), status=Person.Status.ACTIVE)
        self.policy = TimePolicy.objects.create(organization=self.org, overtime_after_hours=Decimal("40.00"))
        ensure_pay_categories(self.org)
        self.categories = {item.kind: item for item in self.org.pay_categories.all()}

    def post(self, officer, hours, day=1, minute_offset=0):
        """A post and its accepted in/out pair, placed inside the *current* workweek.

        Counted from Monday rather than from ``now``: these tests are about which week the hours
        land in, and a fixture that adds days to a Saturday run spills into the next workweek —
        the boundary bug that has already broken this suite twice (see roadmap §11).
        """
        monday = self.now.date() - self.timedelta(days=self.now.date().weekday())
        starts_on = monday + self.timedelta(days=day)
        starts = self.now.replace(year=starts_on.year, month=starts_on.month, day=starts_on.day,
                                  hour=7, minute=0, second=0, microsecond=0) + self.timedelta(minutes=minute_offset)
        shift = Shift.objects.create(organization=self.org, site=self.site, officer=officer,
                                     starts_at=starts, ends_at=starts + self.timedelta(hours=hours),
                                     status=Shift.Status.PUBLISHED, post_name="Gate house")
        for kind, at in ((1, starts), (2, starts + self.timedelta(hours=hours))):
            Punch.objects.create(organization=self.org, person=officer, shift=shift,
                                 kind=Punch.Kind.IN if kind == 1 else Punch.Kind.OUT,
                                 client_event_id=uuid.uuid4(), occurred_at=at)
        return shift

    def designate(self, shift, kind, hours, reason="Officer relieved of duty for the whole period"):
        category = self.categories[kind]
        self.client.force_login(self.clerk)
        return self.client.post(reverse("shift_hours_save", args=[shift.pk]),
                                {"category_id": category.pk, "hours": str(hours), "reason": reason},
                                follow=True)

    def rows(self, start=None, end=None):
        from .services import payroll_rows
        # The window has to contain the Monday the posts are placed on, which on a weekend run is
        # up to six days before `now` — a window starting three days back silently excluded it and
        # every total came out zero, which is the shape of a payroll test that cannot fail.
        monday = self.now.date() - self.timedelta(days=self.now.date().weekday())
        start = start or self.now.replace(year=monday.year, month=monday.month, day=monday.day,
                                           hour=0, minute=0, second=0, microsecond=0) - self.timedelta(days=1)
        end = end or self.now + self.timedelta(days=14)
        rows = payroll_rows(self.org, start, end)
        return rows

    def line(self, rows, category):
        return next((item for item in rows if item["pay_category"] == category), None)

    # -- the three category shapes --

    def test_an_unpaid_meal_leaves_the_tour_and_the_week(self):
        shift = self.post(self.ana, 8)
        self.designate(shift, "break", Decimal("1.00"))
        rows = self.rows()
        worked = self.line(rows, "worked")
        self.assertEqual(worked["total_hours"], Decimal("7.00"))
        self.assertEqual(worked["estimated_pay"], Decimal("140.00"))
        break_row = self.line(rows, "break")
        self.assertEqual(break_row["total_hours"], Decimal("1.00"))
        self.assertEqual(break_row["estimated_pay"], Decimal("0.00"))
        self.assertIn("relieved of duty", break_row["exception"])

    def test_two_posts_and_a_third_that_breaches_the_threshold(self):
        """The point of deducting before the week is totalled: the break hours must be able to
        un-charge overtime on a *later* post, or the firm pays premium for time it never paid for."""
        first = self.post(self.ana, 20, day=1)
        self.post(self.ana, 22, day=2, minute_offset=0)
        before = self.rows()
        overtime_before = sum(item["overtime_hours"] for item in before if item["pay_category"] == "worked")
        self.assertEqual(overtime_before, Decimal("2.00"))
        self.designate(first, "break", Decimal("2.00"))
        after = self.rows()
        overtime_after = sum(item["overtime_hours"] for item in after if item["pay_category"] == "worked")
        self.assertEqual(overtime_after, Decimal("0.00"))

    def test_a_premium_category_prices_only_the_excess_over_straight_time(self):
        shift = self.post(self.ana, 8)
        self.designate(shift, "holiday", Decimal("8.00"))
        self.categories["holiday"].refresh_from_db()
        category = self.categories["holiday"]
        category.multiplier = Decimal("1.50")
        category.save()
        rows = self.rows()
        worked = self.line(rows, "worked")
        premium = self.line(rows, "holiday")
        self.assertEqual(worked["estimated_pay"], Decimal("160.00"))
        self.assertEqual(premium["estimated_pay"], Decimal("80.00"),
                         "8h at half again the rate — the hour is in the worked line already")
        self.assertEqual(premium["total_hours"], Decimal("0.00"),
                         "A premium line carrying hours would count them twice in any total")
        self.assertEqual(sum(item["total_hours"] for item in rows if item["pay_category"] in ("worked", "holiday")),
                         Decimal("8.00"))

    def test_a_paid_category_outside_the_threshold_pays_without_counting(self):
        self.post(self.ana, 38, day=1)
        travel = self.post(self.ana, 4, day=2)
        category = self.categories["travel"]
        category.counts_toward_overtime = False
        category.save()
        self.designate(travel, "travel", Decimal("4.00"))
        rows = self.rows()
        worked = [item for item in rows if item["pay_category"] == "worked"]
        self.assertEqual(sum(item["overtime_hours"] for item in worked), Decimal("0.00"),
                         "38 counted hours do not reach 40 just because 4 more were driven")
        travel_row = self.line(rows, "travel")
        self.assertEqual(travel_row["total_hours"], Decimal("4.00"))
        self.assertEqual(travel_row["estimated_pay"], Decimal("80.00"))

    def test_the_worked_hours_and_the_category_lines_reconcile_to_the_punched_span(self):
        """The invariant that catches every one of the above being subtly wrong: minutes are moved,
        never created or lost."""
        shift = self.post(self.ana, 10)
        self.designate(shift, "break", Decimal("1.00"))
        self.designate(shift, "training", Decimal("2.00"))
        rows = self.rows()
        total = sum(item["raw_hours"] for item in rows if item["pay_category"] != "leave")
        self.assertEqual(total, Decimal("13.00"),
                         "10h punched + 1h unpaid meal + 2h training carved out, counted once each")
        worked = self.line(rows, "worked")
        self.assertEqual(worked["raw_hours"], Decimal("10.00"), "The evidence span never changes")
        # 10h punched, 1h of it unpaid meal (removed from the tour), 2h of training that stays in
        # the worked total and only carries a premium — so the worked line is 9h, not 7h and not 10h.
        self.assertEqual(worked["total_hours"], Decimal("9.00"))

    # -- PAY-5 --

    def test_the_overtime_multiple_comes_from_policy_not_from_arithmetic(self):
        self.policy.overtime_premium = Decimal("2.00")
        self.policy.save()
        self.post(self.ana, 44, day=1)
        worked = self.line(self.rows(), "worked")
        self.assertEqual(worked["overtime_hours"], Decimal("4.00"))
        self.assertEqual(worked["estimated_pay"], Decimal("960.00"),
                         "40h straight at 20 plus 4h at 40 — the old hardcoded 1.5× said 940")

    def test_saving_the_same_premium_twice_does_not_bump_the_version(self):
        before = self.policy.revision
        self.client.force_login(self.clerk)
        self.client.post(reverse("time_policy_premium"),
                         {"overtime_after_hours": "40.00", "overtime_premium": "1.50"})
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.revision, before,
                         "A version that moves for nothing stops meaning anything")
        self.client.post(reverse("time_policy_premium"),
                         {"overtime_after_hours": "40.00", "overtime_premium": "1.75"})
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.revision, before + 1)

    def test_a_payroll_line_names_the_rule_version_that_priced_it(self):
        """Reproducibility, which is the whole reason these are rules and not arithmetic."""
        shift = self.post(self.ana, 8)
        self.designate(shift, "holiday", Decimal("8.00"))
        first = self.line(self.rows(), "holiday")["exception"]
        self.assertIn("v1", first)
        category = self.categories["holiday"]
        category.multiplier = Decimal("1.50")
        category.save()
        self.assertIn("v2", self.line(self.rows(), "holiday")["exception"])

    def test_renaming_a_category_is_not_a_new_rule(self):
        category = self.categories["holiday"]
        before = category.revision
        category.name = "Statutory holiday pay"
        category.save()
        self.client.force_login(self.clerk)
        self.client.post(reverse("pay_category_edit", args=[category.pk]),
                         {"name": "Public holiday pay", "multiplier": "1.00", "paid": "on",
                          "counts_toward_overtime": "on", "active": "on", "interpretation": ""})
        category.refresh_from_db()
        self.assertEqual(category.revision, before)
        self.client.post(reverse("pay_category_edit", args=[category.pk]),
                         {"name": "Public holiday pay", "multiplier": "1.50", "paid": "on",
                          "counts_toward_overtime": "on", "active": "on", "interpretation": "Contract 7 pays time and a half"})
        category.refresh_from_db()
        self.assertEqual(category.revision, before + 1)
        self.assertEqual(str(category.multiplier), "1.50")

    # -- capture discipline --

    def test_a_designation_larger_than_the_post_is_refused(self):
        shift = self.post(self.ana, 8)
        response = self.designate(shift, "break", Decimal("9.00"))
        self.assertTrue(shift.hour_designations.count() == 0)
        self.assertTrue(any("does not fit" in str(item) for item in response.context["messages"]))

    def test_a_designation_without_a_reason_is_refused(self):
        shift = self.post(self.ana, 8)
        self.designate(shift, "travel", Decimal("1.00"), reason="note")
        self.assertEqual(shift.hour_designations.count(), 0)

    def test_redesignating_replaces_the_figure_instead_of_stacking_a_bucket(self):
        shift = self.post(self.ana, 8)
        self.designate(shift, "break", Decimal("1.00"))
        self.designate(shift, "break", Decimal("0.50"))
        self.assertEqual(shift.hour_designations.count(), 1)
        self.assertEqual(shift.hour_designations.first().hours, Decimal("0.50")),
        worked = self.line(self.rows(), "worked")
        self.assertEqual(worked["total_hours"], Decimal("7.50"),
                         "Two stacked claims over the same hour would have deducted 1.5h")

    def test_the_category_lines_do_not_break_the_exporter(self):
        """Every line a category can produce must still carry the columns the customer's payroll
        reads, or the export drops rows silently at the writer."""
        from .services import PAYROLL_EXPORT_FIELDS
        shift = self.post(self.ana, 8)
        self.designate(shift, "break", Decimal("1.00"))
        self.designate(shift, "holiday", Decimal("2.00"))
        for row in self.rows():
            for field in PAYROLL_EXPORT_FIELDS:
                self.assertIn(field, row, f"{row['pay_category']} line is missing {field}")

    def test_the_rules_page_seeds_every_kind_once_and_guards_who_edits_them(self):
        from .services import PAY_CATEGORY_DEFAULTS, ensure_pay_categories
        self.assertEqual(len(ensure_pay_categories(self.org)), 0, "setUp already seeded them")
        # The invariant, not the count: a kind added to the model with no seeded reading would be
        # selectable on the post and then silently skipped by payroll_rows, which prices a
        # designation only when the company has a rule for that kind. A bare number would have let
        # that arrive quietly, and had to be edited the day PAY-2's last category landed.
        self.assertEqual({kind for kind, *_rest in PAY_CATEGORY_DEFAULTS},
                         {kind for kind, _label in PayCategory.Kind.choices},
                         "every designated kind must have a seeded reading")
        self.assertEqual(self.org.pay_categories.count(), len(PayCategory.Kind.choices))
        self.assertEqual(self.org.pay_categories.count(),
                         len({item.kind for item in self.org.pay_categories.all()}), "one row per kind")
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse("settings_pay_categories")).status_code, 403)
        self.client.force_login(self.clerk)
        self.assertEqual(self.client.get(reverse("settings_pay_categories")).status_code, 200)

    def test_a_category_of_another_company_cannot_be_attached_to_a_post(self):
        other = Organization.objects.create(legal_name="Other Gate LLC", display_name="Other", slug="other-pc")
        Membership.objects.create(user=self.worker, organization=other, role=Membership.Role.OWNER)
        other_category = PayCategory.objects.create(organization=other, kind="break", name="Break")
        shift = self.post(self.ana, 8)
        self.client.force_login(self.clerk)
        response = self.client.post(reverse("shift_hours_save", args=[shift.pk]),
                                    {"category_id": other_category.pk, "hours": "1.00",
                                     "reason": "Relieved of duty for the full period"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(shift.hour_designations.count(), 0,
                         "Pricing one tenant's hours on another tenant's multiplier must not be possible")

    # -- provenance: the version a line prints has to be a readable rule --

    def test_the_version_a_category_line_names_is_a_rule_that_can_be_read_back(self):
        """PAY-2's other half. A category line prints "rule v2", and the number is bumped by the
        model's own save so that no door can move it quietly — which means something has to write
        the version down, or the stamp names a rule nobody can produce.
        """
        from .models import RuleRevision
        category = self.categories["holiday"]
        first = RuleRevision.objects.filter(kind=RuleRevision.Kind.PAY_CATEGORY,
                                            rule_id=str(category.pk), revision=1).first()
        self.assertIsNotNone(first, "version 1 is recorded at seeding, not at the first edit")
        self.assertEqual(Decimal(str(first.values["multiplier"])), Decimal("1.00"))
        self.client.force_login(self.clerk)
        self.client.post(reverse("pay_category_edit", args=[category.pk]),
                         {"name": "Holiday hours", "multiplier": "1.50", "paid": "on",
                          "counts_toward_overtime": "on", "active": "on",
                          "interpretation": "Contract 7 pays time and a half on flagged days"})
        category.refresh_from_db()
        self.assertEqual(category.revision, 2)
        history = {item.revision: item for item in RuleRevision.objects.filter(
            kind=RuleRevision.Kind.PAY_CATEGORY, rule_id=str(category.pk))}
        self.assertEqual(set(history), {1, 2},
                         "the version being replaced is stored under its own number, not only the new one")
        self.assertEqual(Decimal(str(history[1].values["multiplier"])), Decimal("1.00"),
                         "v1 has to still say 1.00 after the row moved, or every old stamp is a false statement")
        self.assertEqual(Decimal(str(history[2].values["multiplier"])), Decimal("1.50"))
        self.assertEqual(history[2].changed["multiplier"], {"before": "1.00", "after": "1.50"})

    def test_the_renaming_that_moves_no_number_still_leaves_the_history_alone(self):
        """The companion to the seed test: a version row is written for the current number, so an
        edit that changes only the label must not create a second version claiming a different rule.
        """
        from .models import RuleRevision
        category = self.categories["travel"]
        self.client.force_login(self.clerk)
        self.client.post(reverse("pay_category_edit", args=[category.pk]),
                         {"name": "Travel between sites", "multiplier": "1.00", "paid": "on",
                          "counts_toward_overtime": "on", "active": "on", "interpretation": ""})
        category.refresh_from_db()
        self.assertEqual(category.revision, 1)
        self.assertEqual(RuleRevision.objects.filter(kind=RuleRevision.Kind.PAY_CATEGORY,
                                                     rule_id=str(category.pk)).count(), 1,
                         "a relabel must not manufacture a second version of an unchanged rule")

    def test_the_stored_clock_policy_version_holds_the_overtime_premium(self):
        """PAY-5's own gap: the list that decides *when* the clock policy's version moves gained the
        premium, and the list that decides *what a stored version contains* did not. A stamp could
        then resolve to a snapshot with every rounding field in it and no multiple in it.
        """
        from .services import (bump_policy_revision, ensure_rule_history, record_rule_revision,
                               resolve_rule_version, rule_snapshot)
        ensure_rule_history(self.org)
        resolved = resolve_rule_version(self.org, "company", f"{self.policy.pk}:1")
        self.assertIsNotNone(resolved, "the company baseline needs a readable version 1")
        self.assertIn("overtime_premium", resolved["values"],
                      "a watched value that is never stored is a promise the history cannot keep")
        self.assertEqual(Decimal(str(resolved["values"]["overtime_premium"])), Decimal("1.50"))
        # Moved the way the settings view moves it, because a revision that does not advance has no
        # second version to resolve and the test would prove nothing about the stored values.
        prior_revision = self.policy.revision
        prior_values = rule_snapshot(RuleRevision.Kind.CLOCK_POLICY, self.policy)
        before = {field.name: getattr(self.policy, field.name) for field in self.policy._meta.fields}
        self.policy.overtime_premium = Decimal("1.75")
        bump_policy_revision(self.policy, before)
        self.policy.save()
        self.assertEqual(self.policy.revision, prior_revision + 1, "the premium has to move the version")
        record_rule_revision(self.policy, RuleRevision.Kind.CLOCK_POLICY, None,
                             previous=(prior_revision, prior_values))
        later = resolve_rule_version(self.org, "company", f"{self.policy.pk}:{self.policy.revision}")
        self.assertEqual(Decimal(str(later["values"]["overtime_premium"])), Decimal("1.75"))
        old = resolve_rule_version(self.org, "company", f"{self.policy.pk}:{prior_revision}")
        self.assertEqual(Decimal(str(old["values"]["overtime_premium"])), Decimal("1.50"),
                         "the stamp on a row produced before the edit still reads back the old multiple")

    # -- the sixth kind, and the row that still has no basis --

    def test_a_differential_is_a_premium_on_the_hour_not_extra_hours(self):
        shift = self.post(self.ana, 8)
        self.designate(shift, "differential", Decimal("8.00"))
        category = self.categories["differential"]
        category.multiplier = Decimal("1.10")
        category.save()
        rows = self.rows()
        worked = self.line(rows, "worked")
        premium = self.line(rows, "differential")
        self.assertEqual(worked["estimated_pay"], Decimal("160.00"))
        self.assertEqual(premium["estimated_pay"], Decimal("16.00"),
                         "8h at one-tenth again the rate — the excess over straight time, not the hour")
        self.assertEqual(premium["total_hours"], Decimal("0.00"))
        self.assertEqual(premium["estimated_bill"], "",
                         "the hour is billed by the worked line already; a bill here would invoice it twice")

    def test_approved_leave_is_priced_by_the_firms_own_rule_and_not_by_its_span(self):
        """PAY-2's last open question, closed by the ruling of 2026-10-03: leave pays on the hours it
        displaced, and paidness is the firm's rule with a version — the same shape every other kind got.
        """
        monday = self.now.date() - self.timedelta(days=self.now.date().weekday())
        starts = self.now.replace(year=(monday + self.timedelta(days=9)).year,
                                  month=(monday + self.timedelta(days=9)).month,
                                  day=(monday + self.timedelta(days=9)).day,
                                  hour=7, minute=0, second=0, microsecond=0)
        # A bare published post, not self.post(): that helper writes the punch pair, and an officer who
        # punched the tour worked it — leaving nothing displaced and this row with no money to assert on.
        shift = Shift.objects.create(organization=self.org, site=self.site, officer=self.ana,
                                     starts_at=starts, ends_at=starts + self.timedelta(hours=8),
                                     status=Shift.Status.PUBLISHED, post_name="Gate house")
        leave = TimeOffRequest.objects.create(organization=self.org, person=self.ana,
                                              starts_at=shift.starts_at,
                                              ends_at=shift.starts_at + self.timedelta(hours=8),
                                              status=TimeOffRequest.Status.APPROVED)
        row = self.line(self.rows(), "leave")
        self.assertEqual(row["raw_hours"], Decimal("8.00"), "the displaced post, not the calendar span")
        self.assertEqual(row["estimated_pay"], Decimal("160.00"), "8h at $20 — no punches behind this post")
        self.assertEqual(row["estimated_bill"], "", "an absence is never billed on to the client")
        self.assertIn("8.00 displaced hours across 1 post", row["exception"])
        self.assertIn("rule v1", row["exception"], "the paidness of leave is a versioned rule too")
        self.assertIn("not a payable figure", row["exception"])
        self.assertEqual(leave.status, TimeOffRequest.Status.APPROVED)

        # Turn the rule off and the figure follows it, because the rule is the owner of the number.
        category = self.categories["leave"]
        category.paid = False
        category.save()
        unpaid = self.line(self.rows(), "leave")
        self.assertEqual(unpaid["estimated_pay"], Decimal("0.00"))
        self.assertEqual(unpaid["raw_hours"], Decimal("8.00"),
                         "the hours still count as displaced; only the money changed")
        self.assertIn("leave is marked unpaid here", unpaid["exception"])

    def test_a_leave_that_displaced_nothing_claims_no_money(self):
        """Two different 'no' answers, and the row has to say which one it is: nothing scheduled, or
        everything scheduled was worked anyway. The second is the double-count this rule exists to stop.
        """
        from .services import payroll_rows
        monday = self.now.date() - self.timedelta(days=self.now.date().weekday())
        window_start = self.now.replace(year=monday.year, month=monday.month, day=monday.day,
                                        hour=0, minute=0, second=0, microsecond=0) - self.timedelta(days=1)
        far = self.now.replace(year=monday.year, month=monday.month, day=monday.day,
                               hour=9, minute=0, second=0, microsecond=0) + self.timedelta(days=20)
        TimeOffRequest.objects.create(organization=self.org, person=self.ana, starts_at=far,
                                      ends_at=far + self.timedelta(hours=6),
                                      status=TimeOffRequest.Status.APPROVED)
        row = next((item for item in payroll_rows(self.org, window_start, far + self.timedelta(days=2))
                    if item["pay_category"] == "leave"), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["estimated_pay"], Decimal("0.00"))
        self.assertEqual(row["raw_hours"], Decimal("0.00"))
        self.assertIn("no post was scheduled", row["exception"])

    def test_leave_hours_cannot_be_designated_on_a_post(self):
        """The kind exists so paidness is a rule; it is not a bucket a clerk can mark, because leave
        already arrives from a decided request and a second claim on the same hours pays them twice.
        """
        from .services import set_shift_designation
        shift = self.post(self.ana, 8)
        with self.assertRaises(ValidationError):
            set_shift_designation(shift, self.categories["leave"], Decimal("2.00"),
                                  "Marking it here as well", self.clerk)
        self.assertEqual(shift.hour_designations.count(), 0)

    def test_the_roles_that_price_the_hours_can_reach_the_screen_that_marks_them(self):
        """Reachability. The designation screen shipped with exactly one door — the "Recently
        designated" table on the rules page, which lists only posts somebody has already designated
        on. A clerk whose company has never used it could not discover it, and a control nobody can
        find is a control that is not there.
        """
        from django.contrib.auth import get_user_model
        User = get_user_model()
        shift = self.post(self.ana, 8)
        self.client.force_login(self.clerk)
        payroll_page = self.client.get(reverse("payroll")).content.decode()
        self.assertIn(reverse("settings_pay_categories"), payroll_page,
                      "the payroll page has to offer the rules its figures are priced by")
        # A dispatcher gets to the post itself from the row they are already looking at. The schedule
        # is a managers' page, so this needs the dispatcher's own login, not the clerk's.
        dispatcher = User.objects.create_user(username="pc-sched@example.com", password="pw-pc-sched")
        Membership.objects.create(user=dispatcher, organization=self.org, role=Membership.Role.SCHEDULER)
        self.client.force_login(dispatcher)
        schedule_page = self.client.get(reverse("schedule") + "?layout=list").content.decode()
        self.assertIn(reverse("shift_hours", args=[shift.pk]), schedule_page,
                      "each post on the schedule links to its own hour designations")
        self.assertEqual(self.client.get(reverse("shift_hours", args=[shift.pk])).status_code, 200)

    def test_a_category_version_is_browsable_in_the_same_history_as_every_other_rule(self):
        """The history screen is where "what did v1 say" gets answered, so the new kind has to appear
        in its index rather than be a table nothing reads.
        """
        from .services import revise_pay_category
        category = self.categories["holiday"]
        category.multiplier = Decimal("1.50")
        category.save()
        revise_pay_category(category, self.owner)
        # The history screen is a managers' surface (it is where every rule's versions live), so a
        # payroll clerk who is shown "rule v2" on a line cannot open the answer themselves. Recording
        # that here rather than leaving it as a surprise: an owner can, and widening the gate to
        # PAYROLL is an authorization change, not a bug to fix quietly inside this slice.
        self.client.force_login(self.clerk)
        self.assertEqual(self.client.get(reverse("rule_history")).status_code, 403)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("rule_history_kind", args=[RuleRevision.Kind.PAY_CATEGORY])).content.decode()
        self.assertIn(category.name, page)
        self.assertIn(reverse("rule_history_rule", args=[RuleRevision.Kind.PAY_CATEGORY, str(category.pk)]), page)


class HoldOverTest(TestCase):
    """SCH-3: why a tour ran long, stored where a dispute will look for it.

    The arithmetic tests aim at one thing above all: recording a reason must not move a figure. The
    held-over hours are already inside the punches, so a hold-over that changed ``total_hours`` would
    be paying the same stretch twice — and a reason that only lives in an audit row is a reason nobody
    reads at payroll, which is where the question actually gets asked.
    """

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User = get_user_model()
        self.timedelta = timedelta
        # Local, not UTC. `close_stale_hold_overs` refuses to write an end the clock has not reached,
        # and this fixture's clock-out sits on a day derived from `self.now.date()`. Anchored to
        # `timezone.now()` on a Sunday evening in a UTC-5 zone, that "Tuesday" is next week's Tuesday —
        # still in the future — so the sweep is right and the assertion was wrong. Same family as the
        # workweek-boundary rule in roadmap §11, one boundary over.
        self.now = timezone.localtime().replace(microsecond=0, second=0)
        self.owner = User.objects.create_user(username="ho-owner@example.com", password="pw-ho-owner")
        self.dispatch = User.objects.create_user(username="ho-dispatch@example.com", password="pw-ho-dispatch")
        self.ana_user = User.objects.create_user(username="ho-ana@example.com", password="pw-ho-ana")
        self.org = Organization.objects.create(legal_name="Night Gate LLC", display_name="Night", slug="night-ho")
        for user, role in ((self.owner, Membership.Role.OWNER), (self.dispatch, Membership.Role.SCHEDULER)):
            Membership.objects.create(user=user, organization=self.org, role=role)
        self.client_org = Client.objects.create(organization=self.org, name="Hillcrest")
        self.site = Site.objects.create(organization=self.org, client=self.client_org, name="Gate", address="1 Gate")
        self.other_site = Site.objects.create(organization=self.org, client=self.client_org, name="Yard", address="2 Yard")
        self.ana = Person.objects.create(organization=self.org, first_name="Ana", last_name="Del",
                                         user=self.ana_user, hourly_rate=Decimal("20.00"), status=Person.Status.ACTIVE)
        self.ben = Person.objects.create(organization=self.org, first_name="Ben", last_name="Oro",
                                         hourly_rate=Decimal("21.00"), status=Person.Status.ACTIVE)
        TimePolicy.objects.create(organization=self.org, overtime_after_hours=Decimal("40.00"))

    def post(self, officer, hours, day=1, starts_at_hour=7, site=None):
        """A published post and its accepted pair, placed inside the current workweek.

        Counted from Monday, not from ``now``: adding days to a Saturday runs into the next workweek
        and the totals land in a different period (roadmap §11).
        """
        monday = self.now.date() - self.timedelta(days=self.now.date().weekday())
        starts_on = monday + self.timedelta(days=day)
        starts = self.now.replace(year=starts_on.year, month=starts_on.month, day=starts_on.day,
                                  hour=starts_at_hour, minute=0, second=0, microsecond=0)
        shift = Shift.objects.create(organization=self.org, site=site or self.site, officer=officer,
                                     starts_at=starts, ends_at=starts + self.timedelta(hours=hours),
                                     status=Shift.Status.PUBLISHED, post_name="Gate house")
        for kind, at in ((Punch.Kind.IN, starts), (Punch.Kind.OUT, starts + self.timedelta(hours=hours))):
            Punch.objects.create(organization=self.org, person=officer, shift=shift, kind=kind,
                                 client_event_id=uuid.uuid4(), occurred_at=at)
        return shift

    def punch_late_out(self, shift, hours_past_end):
        """Replace the accepted clock-out with one that lands after the post was due to end."""
        out = shift.punches.filter(kind=Punch.Kind.OUT).first()
        out.occurred_at = shift.ends_at + self.timedelta(hours=hours_past_end)
        out.save(update_fields=["occurred_at"])
        return out

    def rows(self):
        from .services import payroll_rows
        monday = self.now.date() - self.timedelta(days=self.now.date().weekday())
        start = self.now.replace(year=monday.year, month=monday.month, day=monday.day,
                                 hour=0, minute=0, second=0, microsecond=0) - self.timedelta(days=1)
        return payroll_rows(self.org, start, self.now + self.timedelta(days=14))

    def worked(self):
        return next(row for row in self.rows() if row["pay_category"] == "worked")

    # -- the prompt --

    def test_a_tour_that_ran_past_its_end_is_asked_about_before_anybody_remembers(self):
        from .services import overrun_prompt
        shift = self.post(self.ana, 8)
        self.punch_late_out(shift, 1.5)
        prompt = overrun_prompt(shift)
        self.assertFalse(prompt["recorded"])
        self.assertEqual(prompt["hours"], 1.5)
        self.assertIn("no reason recorded", prompt["text"])
        self.assertIn("relief", prompt["text"],
                      "the vertical's own word, so the dispatcher reads a question they recognise")

    def test_an_ordinary_punch_out_is_never_a_prompt(self):
        from .services import overrun_prompt
        shift = self.post(self.ana, 8)
        prompt = overrun_prompt(shift)
        self.assertEqual(prompt["text"], "")
        self.assertEqual(prompt["hours"], 0.0)

    def test_a_post_with_no_clock_out_prompts_nothing(self):
        """The overrun is measured from the clock. Inferring one from the schedule would turn a
        missing punch — which ``punch.missing`` already reports — into a claimed fact about the tour."""
        from .services import overrun_prompt
        shift = self.post(self.ana, 8)
        shift.punches.filter(kind=Punch.Kind.OUT).delete()
        prompt = overrun_prompt(shift)
        self.assertEqual(prompt["text"], "")
        self.assertIsNone(prompt["actual_ends_at"])

    # -- the record, and what it must not touch --

    def test_recording_a_hold_over_answers_the_prompt_without_moving_a_figure(self):
        from .services import overrun_prompt, record_hold_over
        shift = self.post(self.ana, 8)
        self.punch_late_out(shift, 1.5)
        before = self.worked()
        record_hold_over(shift, HoldOver.Reason.RELIEF_NO_SHOW, actor=self.dispatch,
                         held_until=shift.ends_at + self.timedelta(hours=1.5), relief=self.ben)
        after = self.worked()
        prompt = overrun_prompt(shift)
        self.assertTrue(prompt["recorded"])
        self.assertEqual(prompt["text"], "", "the question is answered, so it stops being asked")
        for field in ("raw_hours", "total_hours", "regular_hours", "overtime_hours", "estimated_pay"):
            self.assertEqual(before[field], after[field],
                             f"{field} moved when only a reason was recorded — the held-over hours "
                             "are already in the punches and this would count them twice")

    def test_the_reason_reaches_the_timecard_row(self):
        from .services import record_hold_over
        shift = self.post(self.ana, 8)
        self.punch_late_out(shift, 1.5)
        record_hold_over(shift, HoldOver.Reason.RELIEF_LATE, actor=self.dispatch,
                         held_until=shift.ends_at + self.timedelta(hours=1.5), relief=self.ben)
        exception = self.worked()["exception"]
        self.assertIn("Held over", exception)
        self.assertIn("relief arrived late", exception.lower())
        self.assertIn("Ben Oro", exception, "who was meant to stand it is part of the sentence")
        self.assertIn("1.5h", exception)

    def test_the_prompt_measures_against_the_recorded_end_not_the_one_the_dispatcher_moved(self):
        """The reason a hold-over stores its own ``scheduled_ends_at``: extending the post to cover the
        gap erases the evidence that the tour ran long, and the row has to survive that edit.
        """
        from .services import describe_hold_over, overrun_prompt, record_hold_over
        published_end = None
        shift = self.post(self.ana, 8)
        self.punch_late_out(shift, 1.5)
        published_end = shift.ends_at
        record_hold_over(shift, HoldOver.Reason.UNFILLED, actor=self.dispatch,
                         held_until=published_end + self.timedelta(hours=1.5), scheduled_ends_at=published_end)
        shift.ends_at = published_end + self.timedelta(hours=1.5)
        shift.save(update_fields=["ends_at"])
        row = shift.hold_overs.first()
        self.assertIn("1.5h past", describe_hold_over(row))
        self.assertEqual(overrun_prompt(shift)["text"], "")
        self.assertIn("1.5h past", self.worked()["exception"],
                      "the timecard still says the tour ran past the hour it was published with")
        self.assertEqual(shift.hold_overs.count(), 1)

    # -- refusals --

    def test_the_officer_who_stayed_cannot_be_the_relief_who_missed(self):
        from .services import record_hold_over
        shift = self.post(self.ana, 8)
        with self.assertRaises(ValidationError):
            record_hold_over(shift, HoldOver.Reason.RELIEF_NO_SHOW, actor=self.dispatch,
                             held_until=shift.ends_at + self.timedelta(hours=1), relief=self.ana)

    def test_other_needs_a_note_and_a_time_before_the_end_is_not_a_hold_over(self):
        from .services import record_hold_over
        shift = self.post(self.ana, 8)
        with self.assertRaises(ValidationError):
            record_hold_over(shift, HoldOver.Reason.OTHER, actor=self.dispatch, note="late")
        with self.assertRaises(ValidationError):
            record_hold_over(shift, HoldOver.Reason.RELIEF_LATE, actor=self.dispatch,
                             held_until=shift.ends_at - self.timedelta(minutes=30))
        self.assertEqual(HoldOver.objects.count(), 0, "a refused hold-over leaves no row behind")

    def test_an_open_hold_over_stays_open_until_the_clock_answers_it(self):
        from .services import close_stale_hold_overs, record_hold_over
        shift = self.post(self.ana, 8)
        row = record_hold_over(shift, HoldOver.Reason.RELIEF_NO_SHOW, actor=self.dispatch)
        self.assertIsNone(row.overrun_hours)
        # A `reference` on every call, deliberately. This fixture places its post on Tuesday of the
        # running week (roadmap §11: never `now + N days` for a week-scoped test), which means the tour
        # is genuinely in the future whenever the suite runs on a Monday or a Tuesday — and a sweep
        # driven by the wall clock is then *correctly* unwilling to close it. Passing the reference makes
        # the assertion about the sweep's rule instead of about the day of the week it was run on. The
        # "the clock has not reached it" branch is tested on its own below, with a reference too.
        settled = shift.ends_at + self.timedelta(hours=3)
        self.assertEqual(close_stale_hold_overs(reference=settled), 0,
                         "no clock-out, so nothing can be written")
        self.punch_late_out(shift, 2)
        self.assertEqual(close_stale_hold_overs(reference=settled), 1)
        row.refresh_from_db()
        self.assertEqual(row.overrun_hours, 2.0)
        self.assertEqual(close_stale_hold_overs(reference=settled), 0,
                         "closing twice would rewrite a settled fact")

    def test_the_sweep_refuses_to_invent_an_end_the_clock_has_not_reached(self):
        """The other half of the rule, and the half a wall-clock test can only assert by luck.

        Holding the reference at the scheduled end leaves the late clock-out ahead of it: the tour has
        not finished as far as the sweep is concerned, so writing an end here would be the same
        fabrication `punch.missing` exists to surface instead.
        """
        from .services import close_stale_hold_overs, record_hold_over
        shift = self.post(self.ana, 8)
        row = record_hold_over(shift, HoldOver.Reason.RELIEF_LATE, actor=self.dispatch)
        self.punch_late_out(shift, 2)
        self.assertEqual(close_stale_hold_overs(reference=shift.ends_at), 0,
                         "the sweep closed a tour that had not ended yet at the reference moment")
        row.refresh_from_db()
        self.assertIsNone(row.held_until)

    # -- the split tour --

    def test_a_split_tour_names_the_tour_it_takes_over_at_the_same_site(self):
        first = self.post(self.ana, 8)
        second = Shift.objects.create(organization=self.org, site=self.site, officer=self.ben,
                                      starts_at=first.ends_at, ends_at=first.ends_at + self.timedelta(hours=6),
                                      status=Shift.Status.PUBLISHED, post_name="Gate house",
                                      relief_for=first)
        second.full_clean(exclude=["required_credentials"])
        self.assertEqual(list(first.relief_halves.values_list("pk", flat=True)), [second.pk])

    def test_a_relief_half_at_another_site_or_outside_the_tour_is_refused(self):
        first = self.post(self.ana, 8)
        wrong_site = Shift(organization=self.org, site=self.other_site, officer=self.ben,
                           starts_at=first.ends_at, ends_at=first.ends_at + self.timedelta(hours=6),
                           status=Shift.Status.PUBLISHED, relief_for=first)
        with self.assertRaises(ValidationError):
            wrong_site.full_clean(exclude=["required_credentials"])
        outside = Shift(organization=self.org, site=self.site, officer=self.ben,
                        starts_at=first.ends_at + self.timedelta(hours=5),
                        ends_at=first.ends_at + self.timedelta(hours=11),
                        status=Shift.Status.PUBLISHED, relief_for=first)
        with self.assertRaises(ValidationError):
            outside.full_clean(exclude=["required_credentials"])

    # -- the door --

    def test_a_dispatcher_records_a_hold_over_from_the_post_and_an_officer_cannot(self):
        shift = self.post(self.ana, 8)
        self.punch_late_out(shift, 1.0)
        self.client.force_login(self.ana_user)
        self.assertEqual(self.client.get(reverse("shift_hours", args=[shift.pk])).status_code, 403)
        self.client.force_login(self.dispatch)
        page = self.client.get(reverse("shift_hours", args=[shift.pk])).content.decode()
        self.assertIn("no reason recorded", page, "the prompt is on the page that can answer it")
        response = self.client.post(reverse("shift_hold_over", args=[shift.pk]),
                                    {"reason": HoldOver.Reason.RELIEF_NO_SHOW,
                                     "scheduled_ends_at": shift.ends_at.strftime("%Y-%m-%dT%H:%M"),
                                     "held_until": (shift.ends_at + self.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                                     "relief": str(self.ben.pk), "note": ""}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(shift.hold_overs.count(), 1)
        self.assertEqual(shift.hold_overs.first().recorded_by, self.dispatch)


class InheritedBranchScopeTest(TestCase):
    """AUTH-1: a grant that follows the branch on the person's own file.

    Two paths read a grant — the filters a supervisor acts through, and the recipient list an open
    post is announced to. They are separate code, so a reassignment that moved one and not the other
    would leave a person who cannot open a post but is still told about it. Most of these tests
    therefore check the same move through both doors.
    """

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User = get_user_model()
        self.timedelta = timedelta
        self.now = timezone.now().replace(microsecond=0, second=0)
        self.owner = User.objects.create_user(username="ibs-owner@example.com", password="pw-ibs-owner")
        self.supervisor_user = User.objects.create_user(username="ibs-sup@example.com", password="pw-ibs-sup")
        self.org = Organization.objects.create(legal_name="Two Branch LLC", display_name="Two", slug="two-ibs")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.Role.OWNER)
        Membership.objects.create(user=self.supervisor_user, organization=self.org, role=Membership.Role.SUPERVISOR)
        self.north = Branch.objects.create(organization=self.org, name="North")
        self.south = Branch.objects.create(organization=self.org, name="South")
        self.client = Client.objects.create(organization=self.org, name="Hillcrest")
        self.north_site = Site.objects.create(organization=self.org, client=self.client, name="North gate",
                                              address="1 North", branch=self.north)
        self.south_site = Site.objects.create(organization=self.org, client=self.client, name="South gate",
                                              address="2 South", branch=self.south)
        self.supervisor = Person.objects.create(organization=self.org, first_name="Sam", last_name="Riera",
                                                user=self.supervisor_user, branch=self.north,
                                                status=Person.Status.ACTIVE)
        self.membership = Membership.objects.get(user=self.supervisor_user, organization=self.org)

    def grant(self, **kwargs):
        row = AuthorityScope(membership=self.membership, organization=self.org, **kwargs)
        row.full_clean(exclude=["created_at"])
        row.save()
        return row

    def scope(self):
        from .scope import for_membership
        return for_membership(self.membership)

    def post(self, site):
        return Shift.objects.create(organization=self.org, site=site, starts_at=self.now,
                                    ends_at=self.now + self.timedelta(hours=8),
                                    status=Shift.Status.PUBLISHED, post_name="Gate house")

    def test_an_inherited_grant_reaches_the_branch_on_the_file(self):
        self.grant(follows_own_branch=True)
        scope = self.scope()
        self.assertIn(self.north.id, scope.branch_ids)
        self.assertIn(self.supervisor.id, scope.person_ids,
                      "their own file is in scope too — a supervisor is a person before they are a manager")
        self.assertNotIn(self.south_site.id, scope.site_ids)

    def test_reassigning_the_supervisor_moves_the_reach_with_no_edit_to_the_grant(self):
        """The decision the explicit list dodged: when a guard moves branch, the inherited grant
        moves with them. That is the whole point of the option, and it is the reason it has to be
        chosen deliberately at grant time rather than left as the default.
        """
        self.grant(follows_own_branch=True)
        before = self.scope()
        self.assertIn(self.north_site.id, before.site_ids)
        self.assertNotIn(self.south_site.id, before.site_ids)
        self.supervisor.branch = self.south
        self.supervisor.save(update_fields=["branch"])
        after = self.scope()
        self.assertIn(self.south_site.id, after.site_ids, "the grant followed the file")
        self.assertNotIn(self.north_site.id, after.site_ids,
                         "and did not keep the branch they left — a stale grant is a person with authority over work they no longer supervise")

    def test_the_notice_list_follows_the_same_move(self):
        from .scope import dispatch_recipients_for_shift
        self.grant(follows_own_branch=True)
        north_post, south_post = self.post(self.north_site), self.post(self.south_site)
        self.assertIn(self.supervisor_user.id, dispatch_recipients_for_shift(north_post))
        self.assertNotIn(self.supervisor_user.id, dispatch_recipients_for_shift(south_post))
        self.supervisor.branch = self.south
        self.supervisor.save(update_fields=["branch"])
        self.assertIn(self.supervisor_user.id, dispatch_recipients_for_shift(south_post),
                      "the recipient list and the filters agree about where this person's authority is")
        self.assertNotIn(self.supervisor_user.id, dispatch_recipients_for_shift(north_post))

    def test_a_file_with_no_branch_on_it_covers_nothing_and_says_so(self):
        """Never everything. A grant whose input is missing is an unanswered question, and reading it
        as company-wide authority would turn a data-entry gap into the widest reach in the product.
        """
        self.supervisor.branch = None
        self.supervisor.save(update_fields=["branch"])
        row = self.grant(follows_own_branch=True)
        scope = self.scope()
        self.assertEqual(scope.person_ids, frozenset())
        self.assertEqual(scope.site_ids, frozenset())
        self.assertIn("no branch on the file", row.label)
        self.assertIn("covers nothing", row.label)

    def test_a_grant_cannot_name_a_branch_and_follow_the_file_too(self):
        with self.assertRaises(ValidationError):
            self.grant(follows_own_branch=True, branch=self.north)

    def test_a_second_inherited_grant_is_refused(self):
        self.grant(follows_own_branch=True)
        with self.assertRaises(ValidationError):
            self.grant(follows_own_branch=True)

    def test_an_explicit_branch_grant_still_does_not_move_when_the_file_moves(self):
        """The other half of the choice, pinned so the two options cannot quietly converge: naming a
        branch is a decision about that branch, and a reassignment must not silently re-point it —
        that is what the inherited option is for.
        """
        self.grant(branch=self.north)
        self.supervisor.branch = self.south
        self.supervisor.save(update_fields=["branch"])
        scope = self.scope()
        self.assertIn(self.north_site.id, scope.site_ids)
        self.assertNotIn(self.south_site.id, scope.site_ids)


class ApprovedIdentityDomainTest(TestCase):
    """AUTH-3: a company's own rule about which identities may hold a role here.

    The refusal is the interesting half, so most of these assert what did *not* happen: no
    membership, no verified address, and an invitation still live. A rule that grants the role and
    then complains about it would be worse than no rule at all.
    """

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone
        User = get_user_model()
        self.timedelta = timedelta
        self.User = User
        self.owner = User.objects.create_user(username="did-owner@example.com", password="pw-did-owner")
        self.org = Organization.objects.create(legal_name="Guard Co LLC", display_name="GuardCo", slug="guardco-did")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.Role.OWNER)
        self.now = timezone.now()

    def invite(self, email, role=Membership.Role.HR):
        return MembershipInvitation.issue(organization=self.org, email=email, role=role,
                                          invited_by=self.owner, expires_at=self.now + self.timedelta(days=7))

    def accept(self, invitation, token, as_email=None):
        """Accept as a signed-in account whose address is `as_email` (default: the invited one).

        Every case goes through the existing-account path on purpose. Posting the bare form instead
        would exercise the sign-up form's own validation and report "nothing was granted" for the
        wrong reason — the gate is about the identity, so the identity has to be the variable.
        """
        User = self.User
        email = as_email if as_email is not None else invitation.email
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            user = User.objects.create_user(username=email, email=email, password="pw-did-member")
        self.client.force_login(user)
        return self.client.post(reverse("invitation_accept", args=[token]))

    # -- the default, which is every install today --

    def test_an_empty_allow_list_grants_any_identity(self):
        invitation, token = self.invite("someone@elsewhere.test")
        response = self.accept(invitation, token)
        self.assertRedirects(response, reverse("dashboard"),
                             msg_prefix="an unconfigured rule must not become a rule that refuses everything")
        self.assertTrue(Membership.objects.filter(organization=self.org, user__email="someone@elsewhere.test").exists())

    # -- the refusal, and what it must leave untouched --

    def test_a_role_outside_the_approved_domains_is_refused_without_writing_anything(self):
        self.org.approved_role_domains = ["guardco.com"]
        self.org.save(update_fields=["approved_role_domains"])
        invitation, token = self.invite("roster@personal-mail.test")
        response = self.accept(invitation, token)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Membership.objects.filter(organization=self.org, user__email="roster@personal-mail.test").exists(),
                         "the role is the thing the rule refuses, so none of it may land")
        self.assertFalse(self.org.membership_invitations.filter(pk=invitation.pk, accepted_at__isnull=False).exists(),
                         "a refused invitation stays live: signing in on the right account must be enough to retry")
        self.assertTrue(AuditEvent.objects.filter(action="membership.invitation_refused",
                                                  metadata__reason="approved-identity-domain").exists())

    def test_an_officer_may_still_be_granted_on_a_personal_address(self):
        """The other half of the DD sentence, and the reason this is not a login restriction."""
        self.org.approved_role_domains = ["guardco.com"]
        self.org.save(update_fields=["approved_role_domains"])
        invitation, token = self.invite("ana.delgado@gmail.com", role=Membership.Role.OFFICER)
        response = self.accept(invitation, token)
        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(Membership.objects.filter(organization=self.org, user__email="ana.delgado@gmail.com",
                                                  role=Membership.Role.OFFICER).exists())

    def test_the_list_is_matched_on_the_address_that_will_hold_the_role(self):
        """Not on the invitation string. An account found by username can carry a different email,
        and that email is the identity being granted the role.
        """
        self.org.approved_role_domains = ["guardco.com"]
        self.org.save(update_fields=["approved_role_domains"])
        self.User.objects.create_user(username="ops@guardco.com", email="attacker@elsewhere.test",
                                       password="pw-ops-at-elsewhere")
        invitation, token = self.invite("ops@guardco.com")
        self.client.force_login(self.User.objects.get(username="ops@guardco.com"))
        response = self.client.post(reverse("invitation_accept", args=[token]))
        self.assertEqual(response.status_code, 403,
                         "an address that only resembles an approved one is what this rule exists to refuse")

    # -- configuration, because a rule nobody can set is a deployment variable again --

    def test_the_setting_normalizes_and_refuses_a_value_that_is_not_a_domain(self):
        from .forms import OrganizationSecurityForm
        bad = OrganizationSecurityForm({"mfa_required_roles": [], "role_domains": "east@guardco.com\n"},
                                       instance=self.org)
        self.assertFalse(bad.is_valid(), "a bare address is not a domain and must not be stored as one")
        self.assertIn("not an email domain", str(bad.errors["role_domains"]))
        form = OrganizationSecurityForm({"mfa_required_roles": [],
                                         "role_domains": " @GuardCo.COM , second.co , guardco.com\n"},
                                        instance=self.org)
        self.assertTrue(form.is_valid(), form.errors)
        # Lowercased, @-stripped, de-duplicated and sorted, so saving the same list twice is not a
        # policy change and an audit diff says what actually moved.
        self.assertEqual(form.cleaned_data["role_domains"], ["guardco.com", "second.co"])
        form.save()
        self.org.refresh_from_db()
        self.assertEqual(self.org.approved_role_domains, ["guardco.com", "second.co"])

    def test_a_stored_list_with_odd_casing_still_grants_the_role(self):
        """Normalized at read, because the field is a JSON list a fixture or a shell can also write."""
        self.org.approved_role_domains = [" GuardCo.COM "]
        self.org.save(update_fields=["approved_role_domains"])
        invitation, token = self.invite("hr@guardco.com")
        response = self.accept(invitation, token)
        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(Membership.objects.filter(organization=self.org, user__email="hr@guardco.com").exists())

    def test_a_refusal_does_not_verify_the_address_it_just_rejected(self):
        """The side effect the ordering matters for. Verifying the mailbox is what lets an MFA-required
        role sign in afterwards, so writing it before the identity rule is checked would hand the
        rejected account a verified address and a path into MFA enrolment anyway.
        """
        from allauth.account.models import EmailAddress
        self.org.approved_role_domains = ["guardco.com"]
        self.org.save(update_fields=["approved_role_domains"])
        invitation, token = self.invite("roster@personal-mail.test")
        self.accept(invitation, token)
        self.assertFalse(EmailAddress.objects.filter(email__iexact="roster@personal-mail.test").exists(),
                         "a refused identity must not leave with a verified address behind it")


class PayrollLockSegmentTest(TestCase):
    """PAY-4: a lock that can cover part of a period.

    The assertions all come in pairs, because a partial lock has exactly one failure mode worth
    guarding: the slice you meant to leave alone. So every test states one slice's answer *and* the
    other slice's, and the fallback case is asserted deliberately — an unattributable row takes the
    run's own status, which is the direction that cannot destroy an approved figure.
    """

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User = get_user_model()
        self.timedelta = timedelta
        self.now = timezone.now().replace(microsecond=0, second=0)
        self.owner = User.objects.create_user(username="pl-owner@example.com", password="pw-pl-owner")
        self.clerk = User.objects.create_user(username="pl-clerk@example.com", password="pw-pl-clerk")
        self.org = Organization.objects.create(legal_name="Two Gate LLC", display_name="Gates", slug="gates-pl")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.Role.OWNER)
        Membership.objects.create(user=self.clerk, organization=self.org, role=Membership.Role.PAYROLL)
        self.north = Branch.objects.create(organization=self.org, name="North")
        self.south = Branch.objects.create(organization=self.org, name="South")
        self.account = Client.objects.create(organization=self.org, name="Ridgeline")
        self.other_account = Client.objects.create(organization=self.org, name="Hillcrest")
        self.north_site = Site.objects.create(organization=self.org, client=self.account, name="North gate",
                                             address="1 N", branch=self.north)
        self.south_site = Site.objects.create(organization=self.org, client=self.other_account, name="South gate",
                                              address="2 S", branch=self.south)
        self.period_start = self.now - self.timedelta(days=3)
        self.period_end = self.now - self.timedelta(days=1)
        self.run = PayrollRun.objects.create(organization=self.org, period_start=self.period_start,
                                             period_end=self.period_end, created_by=self.owner,
                                             snapshot=[], exceptions=[])

    def lock(self, *, branch=None, client=None, status="locked", reason="Ridgeline invoice still open on two corrections", by=None):
        from .services import set_payroll_lock_segment
        return set_payroll_lock_segment(self.run, by or self.owner, status, reason,
                                        branch=branch, client=client)

    def approve(self):
        """Approve the period and persist it.

        `payroll_lock_state` reads the run back from the database — the lock answer has to be the same
        for a worker tick as for the screen in front of a person — so setting `.status` on the local
        object and not saving it leaves the gate reading a draft. That is a property of the design, not
        a test bug to paper over, so it is spelled out here.
        """
        self.run.status = PayrollRun.Status.APPROVED
        self.run.save(update_fields=["status"])

    def state(self, at=None, **subject):
        from .services import payroll_lock_state
        return payroll_lock_state(self.org, at or (self.period_start + self.timedelta(hours=6)), **subject)

    # -- the fallback that keeps today's behaviour --

    def test_a_run_with_no_slice_behaves_exactly_as_it_did(self):
        """Backward compatibility, asserted rather than assumed: adding a table under the lock must not
        change any installation that has never used it.
        """
        self.assertFalse(self.state(branch_id=self.north.id, client_id=self.account.id)["locked"])
        self.run.status = PayrollRun.Status.APPROVED
        self.run.save(update_fields=["status"])
        for subject in ({}, {"branch_id": self.north.id}, {"client_id": self.account.id}):
            self.assertTrue(self.state(**subject)["locked"], f"run-level status must still govern {subject}")

    # -- the two directions a slice can differ --

    def test_one_branch_can_be_agreed_while_another_is_still_being_worked(self):
        self.run.status = PayrollRun.Status.DRAFT
        self.lock(branch=self.north, status="locked")
        self.assertTrue(self.state(branch_id=self.north.id, client_id=self.account.id)["locked"])
        self.assertFalse(self.state(branch_id=self.south.id, client_id=self.other_account.id)["locked"],
                         "the branch nobody agreed stays editable — that is the whole feature")

    def test_a_slice_opened_inside_an_approved_period_does_not_unlock_the_rest(self):
        """The case PAY-4 exists for. Reopening one employee's hours used to re-open everybody's; an
        open slice must freeze nothing else and must not make the run a draft.
        """
        self.approve()
        self.lock(branch=self.south, status="open")
        self.assertFalse(self.state(branch_id=self.south.id, client_id=self.other_account.id)["locked"])
        self.assertTrue(self.state(branch_id=self.north.id, client_id=self.account.id)["locked"],
                        "a correction window on South cannot quietly re-open North")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, PayrollRun.Status.APPROVED)

    def test_the_specific_slice_wins_over_the_company_wide_one(self):
        self.lock(status="locked")                        # everywhere else
        self.lock(client=self.account, status="open")    # except this contract
        self.assertFalse(self.state(client_id=self.account.id)["locked"])
        self.assertTrue(self.state(client_id=self.other_account.id)["locked"])

    def test_a_row_that_cannot_be_placed_takes_the_conservative_answer(self):
        """A punch with no post has no branch and no contract. Reading that as "no segment applies, so
        the open slice covers it" would let an unattributable clock event edit an agreed period.
        """
        self.approve()
        self.lock(branch=self.south, status="open")
        self.assertTrue(self.state()["locked"])
        self.assertIn("approved", self.state()["by"])

    # -- the decisions and their limits --

    def test_a_slice_needs_a_reason_and_covers_one_axis_not_two(self):
        from .services import set_payroll_lock_segment
        with self.assertRaises(ValidationError):
            set_payroll_lock_segment(self.run, self.owner, "locked", "short", branch=self.north)
        with self.assertRaises(ValidationError):
            set_payroll_lock_segment(self.run, self.owner, "locked", "both at once is an ambiguity",
                                     branch=self.north, client=self.account)
        self.assertEqual(PayrollLockSegment.objects.count(), 0)

    def test_setting_a_slice_twice_replaces_it_rather_than_leaving_two_answers(self):
        first = self.lock(branch=self.north, status="locked")
        second = self.lock(branch=self.north, status="open")
        self.assertEqual(PayrollLockSegment.objects.count(), 1)
        self.assertEqual(first.pk, second.pk)
        self.assertFalse(self.state(branch_id=self.north.id)["locked"])

    def test_a_period_with_an_open_slice_cannot_be_exported(self):
        """The new mistake a partial lock makes possible: handing the customer a file while part of the
        period is still moving. Named rather than merely refused, because "some of it" is the answer.
        """
        self.run.status = PayrollRun.Status.APPROVED
        self.run.save(update_fields=["status"])
        self.lock(branch=self.south, status="open")
        self.client.force_login(self.clerk)
        response = self.client.get(reverse("payroll_run_export", args=[self.run.pk]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("cannot be exported while a slice of it is open",
                      " ".join(str(item) for item in response.context["messages"]))
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, PayrollRun.Status.APPROVED,
                         "a refused export must not mark the period as sent")

    def test_a_payroll_approver_can_agree_a_slice_but_cannot_open_one(self):
        """Locking is the approver's own act; opening undoes an approval, so it stays owner/admin —
        the same line the whole-period reopen draws.
        """
        self.run.status = PayrollRun.Status.APPROVED
        self.run.save(update_fields=["status"])
        self.client.force_login(self.clerk)
        self.client.post(reverse("payroll_segment_lock", args=[self.run.pk]),
                         {"subject": f"b{self.north.pk}", "status": "locked",
                          "reason": "North is agreed and paid for this period"})
        self.assertEqual(PayrollLockSegment.objects.count(), 1)
        self.client.post(reverse("payroll_segment_lock", args=[self.run.pk]),
                         {"subject": f"b{self.south.pk}", "status": "open",
                          "reason": "South is still missing a punch correction"})
        self.assertEqual(PayrollLockSegment.objects.count(), 1,
                         "the clerk's open was refused, so no second slice exists")
        self.client.force_login(self.owner)
        self.client.post(reverse("payroll_segment_lock", args=[self.run.pk]),
                         {"subject": f"b{self.south.pk}", "status": "open",
                          "reason": "South is still missing a punch correction"})
        self.assertEqual(PayrollLockSegment.objects.count(), 2)

    def test_each_slice_decision_is_audited_with_its_reason(self):
        row = self.lock(branch=self.north, status="locked")
        event = AuditEvent.objects.filter(action="payroll.segment_locked").latest("occurred_at")
        self.assertEqual(event.metadata["subject"], "North branch")
        self.assertEqual(event.metadata["segment"], str(row.pk))
        self.assertIn("invoice", event.metadata["reason"])

    def test_a_correction_in_an_open_slice_is_allowed_and_in_a_locked_one_is_not(self):
        """The gate the officer and the reviewer actually pass through, checked through the view rather
        than the service, because that is where the message is written.
        """
        guard = get_user_model().objects.create_user(username="pl-guard@example.com", password="pw-pl-guard")
        Membership.objects.create(user=guard, organization=self.org, role=Membership.Role.OFFICER)
        person = Person.objects.create(organization=self.org, user=guard, first_name="Rosa", last_name="Vega",
                                       status=Person.Status.ACTIVE)
        at = self.period_start + self.timedelta(hours=6)
        for site in (self.north_site, self.south_site):
            post = Shift.objects.create(organization=self.org, site=site, officer=person, starts_at=at,
                                        ends_at=at + self.timedelta(hours=8), status=Shift.Status.COMPLETED)
            for kind, when in ((Punch.Kind.IN, at), (Punch.Kind.OUT, at + self.timedelta(hours=8))):
                Punch.objects.create(organization=self.org, person=person, shift=post, kind=kind,
                                     occurred_at=when, client_event_id=uuid.uuid4())
        self.approve()
        self.lock(branch=self.south, status="open")
        self.client.force_login(guard)
        north_punch = Punch.objects.get(shift__site=self.north_site, kind=Punch.Kind.IN)
        south_punch = Punch.objects.get(shift__site=self.south_site, kind=Punch.Kind.IN)
        self.client.post(reverse("adjustment_request", args=[north_punch.pk]),
                         {"proposed_at": north_punch.occurred_at, "reason": "Punched a minute early"})
        self.assertEqual(PunchAdjustment.objects.count(), 0,
                         "North is inside an approved period with no slice of its own — still frozen")
        self.client.post(reverse("adjustment_request", args=[south_punch.pk]),
                         {"proposed_at": south_punch.occurred_at, "reason": "Punched a minute early"})
        self.assertEqual(PunchAdjustment.objects.count(), 1,
                         "South was deliberately left open, so its correction is allowed")


class DocumentPreviewTest(TestCase):
    """In-page record viewing, which supersedes "uploads are never served inline".

    Every test here is a question about the *refusal*, because that is what the change risks: a
    preview route one rung more permissive than the download route is a new leak wearing an old
    feature's clothes, and a preview that trusts the uploader's declared type is an XSS with a
    friendly face.
    """

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.utils import timezone
        User = get_user_model()
        self.files = SimpleUploadedFile
        self.now = timezone.now()
        self.owner = User.objects.create_user(username="pv-owner@example.com", password="pw-pv-owner")
        self.worker = User.objects.create_user(username="pv-worker@example.com", password="pw-pv-worker")
        self.stranger = User.objects.create_user(username="pv-stranger@example.com", password="pw-pv-stranger")
        self.org = Organization.objects.create(legal_name="Preview Co LLC", display_name="Preview", slug="preview-pv")
        self.other = Organization.objects.create(legal_name="Elsewhere LLC", display_name="Elsewhere", slug="elsewhere-pv")
        for user, role in ((self.owner, Membership.Role.OWNER), (self.worker, Membership.Role.OFFICER)):
            Membership.objects.create(user=user, organization=self.org, role=role)
        Membership.objects.create(user=self.stranger, organization=self.other, role=Membership.Role.OWNER)
        self.ana = Person.objects.create(organization=self.org, first_name="Ana", last_name="Del",
                                         user=self.worker, status=Person.Status.ACTIVE)
        self.bea = Person.objects.create(organization=self.org, first_name="Bea", last_name="Oro",
                                         status=Person.Status.ACTIVE)
        self.handbook = DocumentType.objects.create(organization=self.org, name="Handbook", code="handbook",
            audience=DocumentType.Audience.WORKFORCE, acknowledgment_required=True, signature_required=True)
        self.claim = DocumentType.objects.create(organization=self.org, name="Injury claim", code="claim",
            audience=DocumentType.Audience.PERSON, sensitivity=DocumentType.Sensitivity.SEALED)
        self.licence_doc = DocumentType.objects.create(organization=self.org, name="Licence copy", code="lic",
            audience=DocumentType.Audience.PERSON)

    def file(self, name="record.pdf", payload=b"%PDF-1.4\nthe handbook text", content_type="application/pdf"):
        return self.files(name, payload, content_type=content_type)

    def store(self, kind, person=None, **upload_kwargs):
        from .services import store_person_document
        return store_person_document(organization=self.org, person=person, document_type=kind,
                                     upload=self.file(**upload_kwargs), actor=self.owner)

    def codes(self, document, user):
        """What each of the two routes answers for one record. The pair is the assertion."""
        self.client.force_login(user)
        return (self.client.get(reverse("document_download", args=[document.pk])).status_code,
                self.client.get(reverse("document_preview", args=[document.pk])).status_code)

    # -- the access rule is one rule --

    def test_preview_admits_exactly_what_download_admits(self):
        own_claim = self.store(self.claim, person=self.ana)
        others_record = self.store(self.licence_doc, person=self.bea)
        handbook = self.store(self.handbook)
        deleted = self.store(self.licence_doc, person=self.ana)
        deleted.deleted_at = self.now; deleted.save(update_fields=["deleted_at"])
        cases = {
            "owner, own-person sealed record": (self.owner, own_claim),
            "subject of a sealed record about them": (self.worker, own_claim),
            "officer reading another worker's file": (self.worker, others_record),
            "officer reading a company record": (self.worker, handbook),
            "soft-deleted row by UUID": (self.owner, deleted),
        }
        for label, (user, document) in cases.items():
            download, preview = self.codes(document, user)
            self.assertEqual(download, preview, f"{label}: the two routes disagree ({download} vs {preview})")

    def test_a_record_from_another_tenant_is_not_a_preview_or_a_download(self):
        # A row that does not exist in this tenant must 404 rather than 403, so the status code does
        # not confirm to an attacker that the UUID is a real record somewhere else.
        document = self.store(self.licence_doc, person=self.ana)
        download, preview = self.codes(document, self.stranger)
        self.assertEqual((404, 404), (download, preview))

    # -- what may be shown, and as what --

    def test_the_served_type_is_the_verified_one_not_the_uploaders_claim(self):
        document = self.store(self.handbook, content_type="text/html")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("document_preview", args=[document.pk]))
        self.assertEqual(200, response.status_code)
        self.assertEqual("application/pdf", response["Content-Type"])
        self.assertNotIn("text/html", response["Content-Type"])
        self.assertIn("inline", response["Content-Disposition"])
        self.assertEqual("nosniff", response["X-Content-Type-Options"])

    def test_the_filename_in_the_response_contains_nothing_the_uploader_wrote(self):
        nasty = self.files("....\\..\\evil.pdf", b"%PDF-1.4\nx", content_type="application/pdf")
        from .services import store_person_document
        document = store_person_document(organization=self.org, person=None, document_type=self.handbook,
                                         upload=nasty, actor=self.owner)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("document_preview", args=[document.pk]))
        disposition = response["Content-Disposition"]
        self.assertNotIn("..", disposition)
        self.assertNotIn("evil", disposition)
        self.assertIn(f"record-{document.sha256[:12]}", disposition)

    def test_bytes_that_no_longer_match_the_row_are_not_served(self):
        """The re-check exists for exactly this: storage mutated after upload, or a row whose claim
        outlived its contents. It must fall back to download, not render.
        """
        document = self.store(self.handbook)
        with document.file.open("wb") as handle:
            handle.write(b"<script>alert(1)</script>")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("document_preview", args=[document.pk]), follow=True)
        self.assertTrue(response.redirect_chain,
                        "a refused preview is a redirect with a reason, never a 200 carrying the bytes")
        self.assertIn("no longer match",
                      " ".join(str(item) for item in response.context["messages"]))
        self.assertEqual(200, self.client.get(reverse("document_download", args=[document.pk])).status_code,
                         "refusing to render is not the same as destroying the record")

    def test_a_type_that_cannot_be_rendered_safely_is_download_only(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.store(self.handbook, name="memo.docx", payload=b"not a zip at all")
        document = self.store(self.handbook, name="memo.docx", payload=b"PK\x03\x04fake zip body")
        document.refresh_from_db()
        self.assertEqual("application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         document.verified_type)
        self.assertIsNone(document.preview_kind)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("document_preview", args=[document.pk]), follow=True)
        self.assertIn("has to be downloaded",
                      " ".join(str(item) for item in response.context["messages"]))

    def test_a_row_stored_before_type_verification_exists_is_not_previewable(self):
        document = self.store(self.handbook)
        PersonDocument.objects.filter(pk=document.pk).update(verified_type="")
        document.refresh_from_db()
        self.assertIsNone(document.preview_kind)
        self.client.force_login(self.owner)
        self.assertEqual(200, self.client.get(reverse("document_download", args=[document.pk])).status_code)
        response = self.client.get(reverse("document_preview", args=[document.pk]), follow=True)
        self.assertIn("has to be downloaded",
                      " ".join(str(item) for item in response.context["messages"]))

    def test_the_preview_response_carries_the_sandbox_policy(self):
        document = self.store(self.handbook)
        self.client.force_login(self.owner)
        policy = self.client.get(reverse("document_preview", args=[document.pk]))["Content-Security-Policy"]
        self.assertIn("script-src 'none'", policy)
        self.assertIn("object-src 'none'", policy)
        self.assertIn("base-uri 'none'", policy)
        self.assertIn("frame-ancestors 'self'", policy,
                      "the modal frames it and nothing else may; the global 'none' is overridden here on purpose")
        self.assertNotIn("frame-ancestors 'none'", policy)

    # -- the signed-link path --

    def test_a_signed_link_is_preferred_and_overrides_the_served_type(self):
        """Asserted against the service, which is where the decision lives.

        Not through the view: `document.file` is a FieldFile whose storage cannot be swapped from a
        test without pretending to be django-storages, and a test that patches the thing it is
        supposed to exercise proves nothing about the thing it patches.
        """
        from .services import PREVIEW_URL_SECONDS, presigned_preview_url
        document = self.store(self.handbook)
        seen = {}

        class Signed:
            querystring_auth = True
            custom_domain = None
            cloudfront_signer = None

            def url(self, name, parameters=None, expire=None):
                seen.update(name=name, parameters=parameters, expire=expire)
                return "https://bucket.example/signed?x=1"

        class Handle:
            storage = Signed()
            name = "records/preview.pdf"

        class Standin:
            file = Handle()
            verified_type = document.verified_type
            sha256 = document.sha256

        url = presigned_preview_url(Standin())
        self.assertEqual("https://bucket.example/signed?x=1", url)
        self.assertEqual("application/pdf", seen["parameters"]["ResponseContentType"],
                         "the override is the whole point: the browser is told our type, not the object's")
        self.assertIn("inline", seen["parameters"]["ResponseContentDisposition"])
        self.assertIn(f"record-{document.sha256[:12]}", seen["parameters"]["ResponseContentDisposition"])
        self.assertEqual(PREVIEW_URL_SECONDS, seen["expire"])
        self.assertLessEqual(seen["expire"], 120,
                             "a preview link outliving two minutes is an authorization window, not a cache")
        # And the real storage on local disk must not produce one, or every deployment would redirect
        # to a URL that was never signed.
        self.assertIsNone(presigned_preview_url(document))

    def test_a_local_storage_streams_the_bytes_itself_after_re_reading_them(self):
        from .services import preview_source
        document = self.store(self.handbook)
        url, handle, content_type, kind = preview_source(document)
        self.assertIsNone(url)
        self.assertEqual("application/pdf", content_type)
        self.assertEqual("pdf", kind)
        self.assertEqual(b"%PDF-1.4\nthe handbook text", handle.read())
        handle.close()

    def test_a_pdf_streams_same_origin_even_when_storage_could_sign_a_link(self):
        """The page's PDF.js reads these bytes with fetch under connect-src 'self'; a redirect to the
        bucket would be a cross-origin read the viewer cannot make.
        """
        from unittest.mock import patch
        from .services import preview_source
        document = self.store(self.handbook)
        with patch("core.services.presigned_preview_url", return_value="https://bucket.example/signed") as signer:
            url, handle, _content_type, kind = preview_source(document)
        handle.close()
        self.assertEqual("pdf", kind)
        self.assertIsNone(url)
        signer.assert_not_called()

    def test_a_custom_domain_without_a_signer_is_never_handed_a_bare_public_link(self):
        """django-storages returns an unsigned URL in this configuration. Using it would replace a
        permission check per request with an unauthenticated public read, so the route must stream.
        """
        from .services import presigned_preview_url
        document = self.store(self.handbook)

        class Unsigned:
            querystring_auth = True
            custom_domain = "files.example.com"
            cloudfront_signer = None

            def url(self, name, parameters=None, expire=None):
                return f"https://{self.custom_domain}/{name}"

        class Handle:
            storage = Unsigned()
            name = "records/preview.pdf"

        class Standin:
            file = Handle()
            verified_type = document.verified_type
            sha256 = document.sha256

        self.assertIsNone(presigned_preview_url(Standin()),
                          "an unsigned public URL must never be the thing a permitted reader is pointed at")

    # -- the friction the item exists for --

    def test_a_worker_can_read_the_handbook_without_leaving_the_signing_page(self):
        document = self.store(self.handbook)
        self.client.force_login(self.worker)
        page = self.client.get(reverse("document_acknowledge", args=[document.pk])).content.decode()
        self.assertIn("Read it here", page)
        self.assertIn(reverse("document_preview", args=[document.pk]), page)
        self.assertIn("Download instead", page, "the old path stays available; it is no longer the only one")

    def test_viewing_is_audited_as_viewing_not_as_downloading(self):
        document = self.store(self.handbook)
        self.client.force_login(self.owner)
        self.client.get(reverse("document_preview", args=[document.pk]))
        event = AuditEvent.objects.filter(action="document.previewed").latest("occurred_at")
        self.assertEqual(str(document.pk), event.target_id)
        self.assertEqual("pdf", event.metadata["kind"])
        self.assertFalse(AuditEvent.objects.filter(action="document.downloaded").exists())


class SharedKioskPinTest(TestCase):
    """CLK-2: a station that many officers stand at, and the PIN that makes a punch attributable.

    The tests aim at the two ways this feature can be worthless rather than at its wiring. First, a
    shared pad must not be able to record one officer's time for another — buddy punching is the
    industry's own name for the problem, so a kiosk that permits it has not solved anything however
    well it works. Second, the credential must survive being *stored* and being *guessed at*: a PIN in
    plain text, a lockout that rolls itself back when the refusal raises, or a station with no bound on
    guessing are each a hole that looks like a feature until somebody walks up to the tablet.
    """

    PIN = "4719"
    OTHER = "8302"

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User = get_user_model()
        self.timedelta = timedelta
        self.now = timezone.now().replace(microsecond=0, second=0)
        self.owner = User.objects.create_user(username="kiosk-owner@example.com", password="pw-kiosk-owner")
        self.scheduler = User.objects.create_user(username="kiosk-dispatch@example.com", password="pw-kiosk-dispatch")
        self.supervisor = User.objects.create_user(username="kiosk-sup@example.com", password="pw-kiosk-sup")
        self.hired = User.objects.create_user(username="kiosk-hr@example.com", password="pw-kiosk-hr")
        self.ana_user = User.objects.create_user(username="kiosk-ana@example.com", password="pw-kiosk-ana")
        self.stranger = User.objects.create_user(username="kiosk-stranger@example.com", password="pw-kiosk-stranger")
        self.org = Organization.objects.create(legal_name="Gate Keepers LLC", display_name="Gates", slug="gate-kiosk")
        for user, role in ((self.owner, Membership.Role.OWNER), (self.scheduler, Membership.Role.SCHEDULER),
                           (self.supervisor, Membership.Role.SUPERVISOR), (self.hired, Membership.Role.HR),
                           (self.ana_user, Membership.Role.OFFICER)):
            Membership.objects.create(user=user, organization=self.org, role=role)
        self.client_org = Client.objects.create(organization=self.org, name="Hillcrest")
        # No coordinates: a geofence the station cannot satisfy would put every punch in review and
        # bury the exception under the thing under test. CLK-4 covers the fence itself.
        self.site = Site.objects.create(organization=self.org, client=self.client_org, name="North Gate", address="1 Gate")
        self.yard = Site.objects.create(organization=self.org, client=self.client_org, name="Yard", address="2 Yard")
        self.ana = Person.objects.create(organization=self.org, first_name="Ana", last_name="Del",
                                         user=self.ana_user, status=Person.Status.ACTIVE)
        self.ben = Person.objects.create(organization=self.org, first_name="Ben", last_name="Oro",
                                         status=Person.Status.ACTIVE)
        TimePolicy.objects.create(organization=self.org)

    def pin(self, person=None, pin=None):
        from .services import set_clock_pin
        return set_clock_pin(person or self.ana, pin or self.PIN)

    def station(self, site=None):
        from .services import open_clock_kiosk
        return open_clock_kiosk(self.org, "Guard shack 2", site=site if site is not None else self.site, actor=self.owner)

    def post(self, officer, starts_offset=-1, hours=8, site=None, status=None):
        """A published post for one officer, spanning the moment the test runs at."""
        starts = self.now + self.timedelta(hours=starts_offset)
        return Shift.objects.create(organization=self.org, site=site or self.site, officer=officer,
            starts_at=starts, ends_at=starts + self.timedelta(hours=hours),
            status=status or Shift.Status.PUBLISHED, post_name="Gate")

    def identify(self, kiosk, pin):
        import json as _json
        return self.client.post(reverse("kiosk_identify", args=[kiosk.pk]),
            data=_json.dumps({"pin": pin}), content_type="application/json")

    def punch(self, kiosk, identity, kind="in", shift=None, checkpoint=None):
        import json as _json
        return self.client.post(reverse("kiosk_punch", args=[kiosk.pk]), data=_json.dumps({
            "identity": identity, "kind": kind, "client_event_id": str(uuid.uuid4()),
            "occurred_at": self.now.isoformat(),
            "shift_id": str(shift.pk) if shift else None,
            "checkpoint_code": str(checkpoint.scan_code) if checkpoint else None}), content_type="application/json")

    # -- the credential --

    def test_a_pin_is_digits_within_a_band_and_not_a_shape_that_guesses_itself(self):
        from .services import validate_clock_pin
        self.assertEqual("4719", validate_clock_pin(self.PIN))
        self.assertEqual("471928", validate_clock_pin("471928"))
        for rejected in ("123", "123456789", "47ab", "", "0000", "1234", "9876"):
            with self.subTest(pin=rejected):
                with self.assertRaises(ValidationError):
                    validate_clock_pin(rejected)

    def test_the_stored_form_of_the_pin_is_never_the_pin(self):
        from .services import clock_pin_index
        person = self.pin()
        self.assertTrue(person.clock_pin.startswith("pbkdf2_"),
                        "a four-digit code is not a secret that survives being read out of a table")
        self.assertNotIn(self.PIN, person.clock_pin)
        self.assertNotIn(self.PIN, person.clock_pin_index)
        self.assertEqual(clock_pin_index(self.org, self.PIN), person.clock_pin_index)
        # The audit row keeps the shape of the event, not the credential.
        event = AuditEvent.objects.filter(action="person.pin_set").latest("occurred_at")
        self.assertEqual({"source", "digits"}, set(event.metadata))
        self.assertNotIn(self.PIN, json.dumps(event.metadata))

    def test_two_officers_cannot_share_one_pin(self):
        from .services import set_clock_pin
        self.pin()
        with self.assertRaises(ValidationError):
            set_clock_pin(self.ben, self.PIN)
        # A different company may well use the same digits: uniqueness is a tenant rule.
        other_org = Organization.objects.create(legal_name="Other LLC", display_name="Other", slug="other-kiosk")
        elsewhere = Person.objects.create(organization=other_org, first_name="Zoe", last_name="Ray",
                                         status=Person.Status.ACTIVE)
        set_clock_pin(elsewhere, self.PIN)
        self.assertEqual(1, Person.objects.filter(organization=self.org, clock_pin_index__gt="").count())

    def test_a_refused_attempt_is_still_counted_after_the_refusal_rolls_back(self):
        """The lockout has to survive the exception it causes.

        ``verify_clock_pin`` raises to say no. If it were wrapped in its own transaction, the
        exception escaping it would roll back the savepoint that just wrote the attempt count — the
        caller catches the raise, so nothing looks wrong while a guesser quietly gets unlimited tries
        and no record that anyone tried. A caller shaped like the real view pins that.
        """
        from django.db import transaction as _transaction
        from .services import verify_clock_pin
        self.pin()
        with _transaction.atomic():
            with self.assertRaises(ValidationError):
                verify_clock_pin(self.ana, "1111")
        self.ana.refresh_from_db()
        self.assertEqual(1, self.ana.pin_failed_attempts)
        self.assertIsNone(self.ana.pin_locked_until)

    def test_five_wrong_tries_pause_the_pin_even_for_the_right_digits(self):
        from .services import PIN_MAX_FAILURES, verify_clock_pin
        self.pin()
        for _ in range(PIN_MAX_FAILURES):
            with self.assertRaises(ValidationError):
                verify_clock_pin(self.ana, "1111")
        self.ana.refresh_from_db()
        self.assertIsNotNone(self.ana.pin_locked_until)
        with self.assertRaises(ValidationError):
            verify_clock_pin(self.ana, self.PIN)          # correct, and still refused
        self.assertTrue(AuditEvent.objects.filter(action="person.pin_locked").exists())

    def test_a_station_resolves_a_pin_to_its_officer_and_says_nothing_more(self):
        from .services import find_person_by_pin
        self.pin()
        self.assertEqual(self.ana.pk, find_person_by_pin(self.org, self.PIN).pk)
        for wrong in ("9999", self.OTHER, "", "abcd"):
            with self.subTest(pin=wrong), self.assertRaises(ValidationError) as caught:
                find_person_by_pin(self.org, wrong)
            self.assertEqual("Clock PIN not recognised.", caught.exception.messages[0],
                             "the pad must not confirm which digits, or which people, exist")

    def test_a_departed_officers_pin_stops_working_at_the_station(self):
        from .services import find_person_by_pin
        self.pin()
        self.ana.status = Person.Status.INACTIVE
        self.ana.save(update_fields=["status"])
        with self.assertRaises(ValidationError) as caught:
            find_person_by_pin(self.org, self.PIN)
        self.assertEqual("Clock PIN not recognised.", caught.exception.messages[0])

    # -- the station --

    def test_a_station_only_ever_offers_the_posts_of_the_officer_who_typed_the_pin(self):
        from .services import kiosk_shifts
        mine = self.post(self.ana)
        theirs = self.post(self.ben, starts_offset=2, site=self.yard)
        offers, recommended = kiosk_shifts(self.ana)
        self.assertEqual([str(mine.pk)], [row["id"] for row in offers])
        self.assertEqual(str(mine.pk), recommended)
        self.assertEqual("on_post", offers[0]["state"])
        mine.status = Shift.Status.CANCELLED
        mine.save(update_fields=["status"])
        self.assertEqual([], kiosk_shifts(self.ana)[0])

    def test_the_station_refuses_to_clock_one_officer_onto_another_officers_post(self):
        """The point of the PIN, stated at the boundary every clock path crosses.

        An officer standing at the pad has a verified identity and can name any post in the request
        body. Without this check, the shared station would be precisely the tool buddy punching needs.
        """
        from .services import record_punch
        self.pin()
        theirs = self.post(self.ben)
        with self.assertRaises(ValidationError) as caught:
            record_punch(organization=self.org, person=self.ana, shift=theirs, client_event_id=uuid.uuid4(),
                         kind=Punch.Kind.IN, occurred_at=self.now)
        self.assertIn("post assigned to that officer", caught.exception.messages[0])
        self.assertFalse(Punch.objects.filter(shift=theirs).exists())

    def test_the_online_path_is_held_to_the_same_rule_as_the_station(self):
        import json as _json
        self.pin()
        theirs = self.post(self.ben)
        self.client.force_login(self.ana_user)
        answer = self.client.post(reverse("punch_api"), data=_json.dumps({
            "client_event_id": str(uuid.uuid4()), "kind": "in", "occurred_at": self.now.isoformat(),
            "shift_id": str(theirs.pk)}), content_type="application/json")
        self.assertEqual(400, answer.status_code)
        self.assertIn("assigned to that officer", answer.json()["error"])

    def test_a_station_punch_carries_the_station_and_the_pin_as_evidence(self):
        kiosk = self.station()
        self.pin()
        shift = self.post(self.ana)
        answer = self.identify(kiosk, self.PIN)
        self.assertEqual(200, answer.status_code)
        identity = answer.json()["identity"]
        # The pad hands back the post to press, not a list to read: the officer's own in-progress post
        # and nothing else.
        self.assertEqual([str(shift.pk)], [row["id"] for row in answer.json()["shifts"]])
        self.assertEqual(str(shift.pk), answer.json()["recommended"])
        recorded = self.punch(kiosk, identity, "in", shift=shift)
        self.assertEqual(201, recorded.status_code)
        punch = Punch.objects.get(pk=recorded.json()["id"])
        self.assertEqual("kiosk", punch.source)
        self.assertEqual(kiosk.pk, punch.device_id)
        self.assertEqual(Punch.Review.ACCEPTED, punch.review_status)
        event = AuditEvent.objects.filter(action="punch.recorded", target_id=str(punch.pk)).latest("occurred_at")
        self.assertEqual({"kiosk": str(kiosk.pk), "kiosk_name": "Guard shack 2", "identified_by": "pin"},
                         event.metadata["evidence"])

    def test_a_station_needs_no_sign_in_for_an_officer_who_has_never_logged_in(self):
        """The business case: a guard with no account still has to open and close a tour."""
        from .services import set_clock_pin
        kiosk = self.station()
        shift = self.post(self.ben)
        set_clock_pin(self.ben, self.OTHER)
        self.assertIsNone(self.ben.user)
        identity = self.identify(kiosk, self.OTHER).json()["identity"]
        answer = self.punch(kiosk, identity, "in", shift=shift)
        self.assertEqual(201, answer.status_code)
        event = AuditEvent.objects.filter(action="punch.recorded").latest("occurred_at")
        self.assertIsNone(event.actor, "the pad has no user to blame, and inventing one would be a false attribution")
        self.assertEqual("pin", event.metadata["evidence"]["identified_by"])

    def test_an_identity_proof_does_not_travel_to_another_station_or_outlive_the_window(self):
        from django.core import signing
        from unittest import mock
        from .services import kiosk_identity, kiosk_identity_token
        first = self.station()
        second = ClockKiosk.objects.create(organization=self.org, name="East Gate", site=self.yard)
        self.pin()
        token = kiosk_identity_token(first, self.ana)
        self.assertEqual(self.ana.pk, kiosk_identity(token, first).pk)
        with self.assertRaises(signing.BadSignature):
            kiosk_identity("not-a-token", first)
        with self.assertRaises(ValidationError):
            kiosk_identity(token, second)
        # The window itself, rather than a sleep: a negative age makes any timestamp expired, which is
        # the same branch the ninety-second check takes when an officer lingers too long at the pad.
        with mock.patch("core.services.KIOSK_IDENTITY_SECONDS", -1):
            with self.assertRaises(signing.BadSignature):
                kiosk_identity(token, first)

    def test_a_station_refuses_a_forged_or_aged_identity_on_the_punch_route(self):
        import json as _json
        kiosk = self.station()
        shift = self.post(self.ana)
        forged = self.punch(kiosk, "any-old-thing", "in", shift=shift)
        self.assertEqual(401, forged.status_code)
        self.assertEqual("That PIN session has ended. Enter the PIN again.", forged.json()["error"])
        self.assertFalse(Punch.objects.exists())

    def test_a_station_refuses_every_punch_once_the_policy_takes_the_allowance_away(self):
        kiosk = self.station()
        self.pin()
        identity = self.identify(kiosk, self.PIN).json()["identity"]
        TimePolicy.objects.filter(organization=self.org).update(allow_kiosk=False)
        refused = self.punch(kiosk, identity, "in")
        self.assertEqual(400, refused.status_code)
        self.assertIn("does not allow a shared clock station", refused.json()["error"])
        self.assertFalse(Punch.objects.filter(source="kiosk").exists())

    def test_a_contract_or_site_rule_can_forbid_the_station_while_the_company_allows_it(self):
        from .services import effective_clock_policy, kiosk_policy_refusal
        self.assertTrue(effective_clock_policy(self.org, self.site).kiosk["allowed"])
        TimePolicyOverride.objects.create(organization=self.org, client=self.client_org, allow_kiosk=False)
        self.assertEqual(
            "Hillcrest's contract does not allow a shared clock station. Each officer must clock from "
            "their own device on this contract.", kiosk_policy_refusal(self.org, self.site))
        # The contract's refusal is beatable by the site's own yes, which is what "may strengthen or
        # weaken the global baseline" has to mean field by field.
        TimePolicyOverride.objects.create(organization=self.org, site=self.yard, allow_kiosk=True)
        self.assertIsNone(kiosk_policy_refusal(self.org, self.yard))
        self.assertEqual("contract", effective_clock_policy(self.org, self.site).kiosk["source"])

    def test_opening_a_station_at_a_forbidden_post_is_refused(self):
        from .services import kiosk_policy_refusal, open_clock_kiosk
        TimePolicyOverride.objects.create(organization=self.org, site=self.yard, allow_kiosk=False)
        with self.assertRaises(ValidationError):
            open_clock_kiosk(self.org, "Yard tablet", site=self.yard, actor=self.owner)
        self.assertFalse(ClockKiosk.objects.filter(name="Yard tablet").exists())
        self.assertIsNotNone(kiosk_policy_refusal(self.org, self.yard))

    def test_an_explicit_no_at_a_site_survives_the_edit_screen_as_no_rather_than_as_inherit(self):
        """The tri-state trap the roadmap names, on the field CLK-2 adds.

        A plain BooleanField in the override form would turn "not decided" and "forbidden" into the same
        False, so every site that never chose anything would silently forbid its own station — and an
        editor that rendered False as *inherit* would hide the refusal from the manager who has to lift
        it. Both halves are pinned here: the form's mapping and the page that shows it.
        """
        row = TimePolicyOverride.objects.create(organization=self.org, site=self.site, allow_kiosk=False)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("time_policy_override", args=[row.pk])).content.decode()
        self.assertIn('value="no"', page)
        self.client.post(reverse("time_policy_override", args=[row.pk]), {"client": "", "site": self.site.pk,
            "require_geofence": "", "allow_kiosk": "no", "rounding_mode": "", "rounding_minutes": ""})
        row.refresh_from_db()
        self.assertFalse(row.allow_kiosk)
        self.assertIn("allow_kiosk", row.overridden_fields())
        # And inherit is still reachable: an empty choice is None, not False.
        self.client.post(reverse("time_policy_override", args=[row.pk]), {"client": "", "site": self.site.pk,
            "require_geofence": "", "allow_kiosk": "", "rounding_mode": "", "rounding_minutes": ""})
        row.refresh_from_db()
        self.assertIsNone(row.allow_kiosk)

    def test_the_station_pauses_after_a_burst_of_refused_pins_and_a_manager_unpauses_it(self):
        """The bound that makes a four-digit code honest.

        A refused guess names no officer, so nothing can be charged to a person; the station carries
        it instead. Without a bound at all, a tablet reachable from the parking lot is a PIN oracle.
        """
        from .services import KIOSK_FAILURE_LIMIT, clear_kiosk_pin_failures, kiosk_pin_throttled
        kiosk = self.station()
        for _ in range(KIOSK_FAILURE_LIMIT):
            self.assertEqual(401, self.identify(kiosk, "9999").status_code)
        self.assertTrue(kiosk_pin_throttled(kiosk))
        # The right PIN is now refused too — a paused station cannot tell an honest guard from the
        # next guess, and guessing its way back in is exactly what the pause is for.
        self.pin()
        paused = self.identify(kiosk, self.PIN)
        self.assertEqual(401, paused.status_code)
        self.assertIn("paused", paused.json()["error"])
        self.assertTrue(AuditEvent.objects.filter(action="clock_kiosk.pin_throttled").exists())
        clear_kiosk_pin_failures(kiosk, self.owner)
        self.assertEqual(200, self.identify(kiosk, self.PIN).status_code)

    def test_a_station_page_is_reachable_by_nobody_signed_in_and_leaks_no_roster(self):
        kiosk = self.station()
        self.pin()
        self.post(self.ben)
        page = self.client.get(reverse("kiosk", args=[kiosk.pk]))
        self.assertEqual(200, page.status_code)
        self.assertIn("Guard shack 2", page.content.decode())
        # The post's own name belongs on the pad — a guard needs to see they are at Hillcrest North
        # Gate and not the yard one — but no officer's does, and no officer's is until their PIN.
        for hidden in ("Ben", "Oro", "Ana", "Del"):
            self.assertNotIn(hidden, page.content.decode(),
                             "the pad alone is on screen; a name only ever appears after that name's PIN")
        self.assertEqual("private, no-store", page.headers["Cache-Control"])

    def test_the_station_endpoints_are_not_left_without_a_csrf_token(self):
        """The offline sync route is exempt because a device token carries it. This one has no such
        credential — the pad is an ordinary same-origin page — so leaving CSRF off would let any
        webpage a tablet ever visits push punches through it."""
        from django.test import Client
        kiosk = self.station()
        api = Client(enforce_csrf_checks=True)
        page = api.get(reverse("kiosk", args=[kiosk.pk]))
        self.pin()
        body = json.dumps({"pin": self.PIN})
        address = reverse("kiosk_identify", args=[kiosk.pk])
        bare = api.post(address, data=body, content_type="application/json")
        self.assertEqual(403, bare.status_code)
        carried = api.post(address, data=body, content_type="application/json",
                           HTTP_X_CSRFTOKEN=page.cookies["csrftoken"].value)
        self.assertEqual(200, carried.status_code)

    def test_a_retired_station_says_so_and_refuses_work(self):
        from .services import close_clock_kiosk
        kiosk = self.station()
        self.pin()
        shift = self.post(self.ana)
        identity = self.identify(kiosk, self.PIN).json()["identity"]
        taken = self.punch(kiosk, identity, "in", shift=shift)
        self.assertEqual(201, taken.status_code)
        close_clock_kiosk(kiosk, self.owner)
        page = self.client.get(reverse("kiosk", args=[kiosk.pk]))
        self.assertIn("retired", page.content.decode())
        self.assertEqual(410, self.identify(kiosk, self.PIN).status_code)
        self.assertEqual(410, self.punch(kiosk, identity, "out", shift=shift).status_code)
        # Retiring a station must not retire the evidence it already took: the punches keep their
        # device_id and the review screen still says where they came from.
        self.assertTrue(Punch.objects.filter(device_id=kiosk.pk).exists())
        self.client.force_login(self.owner)
        self.assertIn("Guard shack 2", self.client.get(reverse("time_review"), {"punches": "all"}).content.decode())

    def test_a_patrol_scan_at_the_station_is_attributed_to_the_point_and_the_post(self):
        kiosk = self.station(site=self.site)
        self.pin()
        shift = self.post(self.ana)
        point = Checkpoint.objects.create(organization=self.org, site=self.site, name="Lobby")
        identity = self.identify(kiosk, self.PIN).json()["identity"]
        answer = self.punch(kiosk, identity, "checkpoint", shift=shift, checkpoint=point)
        self.assertEqual(201, answer.status_code)
        punch = Punch.objects.get(pk=answer.json()["id"])
        self.assertEqual(point.pk, punch.checkpoint_id)
        self.assertEqual(Punch.Kind.CHECKPOINT, punch.kind)
        # A scan taken at the station is still a scan at the point: the station says where the officer
        # was standing, the checkpoint says where they walked to, and neither is a substitute.
        self.assertEqual(kiosk.pk, punch.device_id)

    def test_only_the_rungs_that_hold_a_personnel_file_may_hand_one_out(self):
        # The station register is not public: a station's address is not a secret to the officers who
        # use it, but the list of every post the company clocks at is a picture of the operation.
        self.assertEqual(302, self.client.get(reverse("clock_kiosks")).status_code)
        self.client.force_login(self.stranger)            # an account in no organization at all
        self.assertEqual(403, self.client.get(reverse("clock_kiosks")).status_code)
        self.client.force_login(self.ana_user)            # an officer in this one
        self.assertEqual(403, self.client.get(reverse("clock_kiosks")).status_code)
        self.assertEqual(403, self.client.get(reverse("person_pin_issue", args=[self.ben.pk])).status_code)
        self.assertEqual(200, self.client.get(reverse("clock_pin")).status_code)
        # A dispatcher may stand a station at a post; issuing the credential that authenticates time
        # on that post is the rung above them, because that is where a shared clock starts recording
        # whoever the supervisor likes.
        self.client.force_login(self.scheduler)
        self.assertEqual(200, self.client.get(reverse("clock_kiosks")).status_code)
        self.assertEqual(403, self.client.get(reverse("person_pin_issue", args=[self.ben.pk])).status_code)
        self.client.force_login(self.supervisor)
        self.assertEqual(403, self.client.get(reverse("person_pin_issue", args=[self.ben.pk])).status_code)
        self.client.force_login(self.hired)
        self.assertEqual(200, self.client.get(reverse("person_pin_issue", args=[self.ben.pk])).status_code)
        self.assertEqual(200, self.client.get(reverse("clock_kiosks")).status_code)

    def test_an_officer_sets_their_own_pin_and_must_know_the_old_one_to_change_it(self):
        from .services import clock_pin_index
        self.client.force_login(self.ana_user)
        page = self.client.get(reverse("clock_pin"))
        self.assertIn("Set your clock PIN", page.content.decode())
        self.client.post(reverse("clock_pin"), {"pin": self.PIN, "confirm": self.PIN})
        self.ana.refresh_from_db()
        first_index = self.ana.clock_pin_index
        self.assertEqual(clock_pin_index(self.org, self.PIN), first_index)
        # Now a change needs the current one, and a wrong one is refused without setting anything.
        refused = self.client.post(reverse("clock_pin"), {"current_pin": "9999", "pin": self.OTHER, "confirm": self.OTHER})
        self.assertContains(refused, "Clock PIN not recognised.")
        self.ana.refresh_from_db()
        self.assertEqual(first_index, self.ana.clock_pin_index, "the refusal must not have reissued anything")
        self.assertEqual(1, self.ana.pin_failed_attempts)
        self.client.post(reverse("clock_pin"), {"current_pin": self.PIN, "pin": self.OTHER, "confirm": self.OTHER})
        self.ana.refresh_from_db()
        self.assertEqual(clock_pin_index(self.org, self.OTHER), self.ana.clock_pin_index)
        self.assertEqual(0, self.ana.pin_failed_attempts, "a good PIN clears the count it was under")
        self.assertEqual(2, AuditEvent.objects.filter(action="person.pin_set").count())

    def test_the_review_screen_names_the_station_a_punch_came_off(self):
        kiosk = self.station()
        self.pin()
        shift = self.post(self.ana)
        identity = self.identify(kiosk, self.PIN).json()["identity"]
        self.punch(kiosk, identity, "in", shift=shift)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("time_review"), {"punches": "all"}).content.decode()
        self.assertIn("Guard shack 2", page)
        self.assertIn("PIN-verified", page)

    def test_the_clock_page_tells_an_officer_whether_they_can_use_a_station_yet(self):
        self.client.force_login(self.ana_user)
        lonely = self.client.get(reverse("clock")).content.decode()
        self.assertIn("Set my clock PIN", lonely)
        self.pin()
        kiosk = self.station()
        armed = self.client.get(reverse("clock")).content.decode()
        self.assertIn("Guard shack 2", armed)
        self.assertIn(reverse("kiosk", args=[kiosk.pk]), armed)
        self.assertNotIn("Set my clock PIN", armed)


class AuditRetentionSealTest(TestCase):
    """REC-4: the retention setting was displayed and enforced by nothing, and a hash chain cannot simply
    have rows removed from its beginning.

    The tests aim at the one property the whole design rests on: after a purge, an honest reader must
    still be able to tell a *accounted-for* gap from a tampered one. Everything else — the archive, the
    digests, the two-step order — exists to keep that distinction true. A bulk ``DELETE`` that "worked"
    would be the failure here, not the success.
    """

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User = get_user_model()
        self.timedelta = timedelta
        self.now = timezone.now().replace(microsecond=0, second=0)
        self.owner = User.objects.create_user(username="seal-owner@example.com", password="pw-seal-owner")
        self.auditor = User.objects.create_user(username="seal-auditor@example.com", password="pw-seal-auditor")
        self.officer = User.objects.create_user(username="seal-officer@example.com", password="pw-seal-officer")
        self.org = Organization.objects.create(legal_name="Seal Co", display_name="Seal", slug="seal-co",
                                               audit_retention_days=90)
        for user, role in ((self.owner, Membership.Role.OWNER), (self.auditor, Membership.Role.AUDITOR),
                           (self.officer, Membership.Role.OFFICER)):
            Membership.objects.create(user=user, organization=self.org, role=role)

    def backdate(self, action, when, metadata=None):
        """One event that already happened, hashed exactly as the live writer would hash it.

        ``occurred_at`` is ``auto_now_add`` and every later edit path is refused — by the queryset, the
        model, and on MySQL by the trigger — so a retention test needs history planted, not corrected.
        Rows are inserted oldest first, which is what makes the chain walk reproducible.
        """
        from django.db import connection
        from .models import audit_event_hash
        pk = uuid.uuid4()
        # Linked to the row the *verification walk* will see last, not to the row `AuditEvent.save`
        # would have picked: the writer orders ties by descending id and the reader by ascending id, and
        # a planted chain has to follow the reader or every test here would report a false break.
        head = AuditEvent.objects.filter(organization=self.org).order_by("occurred_at", "id").last()
        previous = head.event_hash if head else ""
        digest = audit_event_hash(id=pk, organization=self.org.pk, actor=None, action=action,
            target_type="test", target_id="row", metadata=metadata or {}, previous_hash=previous)
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core_auditevent (id, organization_id, actor_id, action, target_type, target_id,"
                " metadata, previous_hash, event_hash, occurred_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [pk.hex, self.org.pk.hex, None, action, "test", "row", json.dumps(metadata or {}),
                 previous, digest, self._sql(when)])
        return AuditEvent.objects.get(pk=pk)

    @staticmethod
    def _sql(value):
        from datetime import timezone
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    def months_ago(self, count, day=12):
        from django.utils import timezone
        today = timezone.now().replace(day=day, hour=9, minute=0, second=0, microsecond=0)
        year, month = today.year, today.month - count
        while month <= 0:
            month += 12; year -= 1
        return today.replace(year=year, month=month)

    def history(self):
        """Three fully closed months past the window, two events each, plus two still inside it.

        20 July sits in the same month as the 90-day cutoff and 3 October is today's month, so neither is
        a closed period. A retention job that trimmed the month still running would erase the evidence of
        the thing being investigated right now.
        """
        rows = []
        for back in (6, 5, 4):                                   # Apr, May, Jun from 4 October
            for offset, action in enumerate(("punch.recorded", "payroll.locked")):
                # Distinct days, not just distinct rows: two events sharing one timestamp are ordered by
                # their random uuids, and a planted chain that links them in insertion order would be a
                # break in the walk order half the time.
                rows.append(self.backdate(action, self.months_ago(back, day=11 + offset)))
        for back, day in ((3, 20), (0, 3)):                      # 20 Jul is inside the cutoff month
            rows.append(self.backdate("document.signed", self.months_ago(back, day=day)))
        return rows

    # -- what is due --

    def test_only_whole_closed_months_past_the_window_are_due(self):
        from .services import audit_periods_due
        self.history()
        periods, note = audit_periods_due(self.org)
        self.assertIsNone(note)
        self.assertTrue(periods, "three closed months of history under a 90-day window has to be due")
        self.assertEqual(3, len(periods))
        self.assertEqual(periods[0][0], periods[0][0].replace(day=1, hour=0, minute=0, second=0, microsecond=0))
        for start, end in periods:
            self.assertLess(start, end)
        # Contiguity: each period starts where the one before it ended, because a seal that skips rows
        # is the gap a later reader cannot tell from tampering.
        for earlier, later in zip(periods, periods[1:]):
            self.assertEqual(earlier[1], later[0])
        # Nothing in the running month is ever due, and the last period stops at the boundary.
        self.assertLessEqual(periods[-1][1], self.now - self.timedelta(days=90))
        self.assertEqual(2, AuditEvent.objects.filter(occurred_at__gte=periods[-1][1]).count())

    def test_a_window_below_the_floor_is_refused_rather_than_silently_raised(self):
        from .services import audit_periods_due
        self.history()
        self.org.audit_retention_days = 30
        self.org.save(update_fields=["audit_retention_days"])
        periods, note = audit_periods_due(self.org)
        self.assertEqual([], periods)
        self.assertIn("floor", note)
        # The alternative — quietly treating 30 as 90 — would tell an owner their history is kept for a
        # quarter when they set a month, and the alternative of honouring 30 would be a wipe.
        self.assertEqual(0, AuditSeal.objects.count())

    # -- sealing --

    def test_a_sealed_period_records_the_head_the_chain_continues_from(self):
        from .services import audit_periods_due, seal_audit_period, verify_seal_archive
        self.history()
        start, end = audit_periods_due(self.org)[0][0]
        seal = seal_audit_period(self.org, start, end, actor=self.owner)
        rows = AuditEvent.objects.filter(occurred_at__gte=start, occurred_at__lt=end).order_by("occurred_at", "id")
        self.assertEqual(rows.count(), seal.event_count)
        self.assertEqual(rows.first().event_hash, seal.first_hash)
        self.assertEqual(rows.last().event_hash, seal.last_hash)
        self.assertEqual(rows.first().previous_hash, seal.first_previous)
        self.assertEqual(0, seal.event_count - len(rows))
        checked = verify_seal_archive(seal)
        self.assertEqual([], checked["problems"])
        self.assertEqual(seal.event_count, checked["events"])
        self.assertTrue(AuditEvent.objects.filter(action="audit.sealed", target_id=str(seal.pk)).exists())
        # Sealing is not deleting: every row is still where it was.
        self.assertEqual("sealed", seal.status)

    def test_a_seal_refuses_to_be_written_over_a_chain_that_does_not_verify(self):
        """The order is the safety property. A seal gives a missing head an official explanation, so it
        must be impossible to obtain one while the chain is broken for any other reason."""
        from django.db import connection
        from .services import audit_periods_due, seal_audit_period, verify_audit_chain
        rows = self.history()
        self.assertEqual([], verify_audit_chain(self.org))
        start, end = audit_periods_due(self.org)[0][0]
        if connection.vendor == "mysql":
            self.skipTest("the UPDATE guard in migration 0018 makes this state unreachable on MySQL")
        with connection.cursor() as cursor:
            cursor.execute("UPDATE core_auditevent SET metadata=%s WHERE id=%s",
                           ['{"safe": "no"}', rows[1].pk.hex])
        self.assertEqual([str(rows[1].pk)], verify_audit_chain(self.org))
        with self.assertRaises(ValidationError) as caught:
            seal_audit_period(self.org, start, end, actor=self.owner)
        self.assertIn("does not verify", caught.exception.messages[0])
        self.assertEqual(0, AuditSeal.objects.count())

    def test_two_seals_have_to_meet_exactly_at_the_seam(self):
        from .services import audit_periods_due, seal_audit_period
        self.history()
        periods = audit_periods_due(self.org)[0]
        first = seal_audit_period(self.org, *periods[0], actor=self.owner)
        # Skipping a period is the failure mode: the second seal's first_previous would name a row that
        # is neither live nor archived, and the chain would have an unaccounted hole in it.
        with self.assertRaises(ValidationError) as caught:
            seal_audit_period(self.org, *periods[2], actor=self.owner)
        self.assertIn("continue the period before it", caught.exception.messages[0])
        second = seal_audit_period(self.org, *periods[1], actor=self.owner)
        self.assertEqual(first.last_hash, second.first_previous)

    # -- purging --

    def test_purge_is_refused_until_the_archive_reads_back_as_sealed(self):
        from django.db import connection
        from .services import audit_periods_due, purge_sealed_audit, seal_audit_period
        self.history()
        start, end = audit_periods_due(self.org)[0][0]
        seal = seal_audit_period(self.org, start, end, actor=self.owner)
        live_before = AuditEvent.objects.count()
        # One byte changed in the store and the whole period becomes unpurgable: the archive is the only
        # copy after the trim, so it has to be provably the copy that was written.
        with connection.cursor() as cursor:
            cursor.execute("UPDATE core_auditseal SET archive_sha256=%s WHERE id=%s", ["0" * 64, seal.pk.hex])
        seal.refresh_from_db()
        with self.assertRaises(ValidationError) as caught:
            purge_sealed_audit(seal, self.owner)
        self.assertIn("no longer hashes", caught.exception.messages[0])
        self.assertEqual(live_before, AuditEvent.objects.count())
        self.assertEqual("sealed", seal.status)

    def test_a_period_holding_a_redaction_decision_cannot_be_purged(self):
        from .services import audit_periods_due, purge_sealed_audit, seal_audit_period
        rows = self.history()
        AuditRedaction.objects.create(organization=self.org, event=rows[0], fields=["safe"],
            reason="Names of a complainant in the exported metadata.", legal_basis="Texas Occupations Code ch. 1702",
            requested_by=self.owner)
        start, end = audit_periods_due(self.org)[0][0]
        seal = seal_audit_period(self.org, start, end, actor=self.owner)
        self.assertEqual(1, seal.redaction_count)
        with self.assertRaises(ValidationError) as caught:
            purge_sealed_audit(seal, self.owner)
        self.assertIn("redaction decision", caught.exception.messages[0])
        self.assertTrue(AuditEvent.objects.filter(pk=rows[0].pk).exists())

    def test_a_seal_and_the_table_must_agree_on_what_is_being_deleted(self):
        from .services import audit_periods_due, purge_sealed_audit, seal_audit_period
        self.history()
        start, end = audit_periods_due(self.org)[0][0]
        seal = seal_audit_period(self.org, start, end, actor=self.owner)
        AuditSeal.objects.filter(pk=seal.pk).update(event_count=seal.event_count + 5)
        seal.refresh_from_db()
        with self.assertRaises(ValidationError) as caught:
            purge_sealed_audit(seal, self.owner)
        self.assertIn("disagree about what is being deleted", caught.exception.messages[0])

    def test_after_a_purge_the_chain_still_verifies_and_names_the_seal_that_carries_it(self):
        from .services import audit_periods_due, purge_sealed_audit, seal_audit_period, verify_audit_chain
        self.history()
        start, end = audit_periods_due(self.org)[0][0]
        seal = seal_audit_period(self.org, start, end, actor=self.owner)
        sealed = seal.event_count
        purge_sealed_audit(seal, self.owner)
        seal.refresh_from_db()
        self.assertEqual("purged", seal.status)
        self.assertIsNotNone(seal.purged_at)
        self.assertEqual(0, AuditEvent.objects.filter(occurred_at__lt=end).count())
        self.assertEqual([], verify_audit_chain(self.org),
                         "a trimmed chain that still verifies is the whole point of sealing first")
        self.assertTrue(AuditEvent.objects.filter(action="audit.purged").exists())
        # The head it continues from is the seal's own, not a hash somebody typed in later.
        surviving = AuditEvent.objects.order_by("occurred_at", "id").first()
        self.assertEqual(seal.last_hash, surviving.previous_hash)

    def test_a_forged_seal_does_not_excuse_a_genuine_break(self):
        """The head lookup has to be a claim the surviving row makes, not a permission slip."""
        from django.db import connection
        from .services import audit_periods_due, seal_audit_period, verify_audit_chain
        rows = self.history()
        start, end = audit_periods_due(self.org)[0][0]
        seal = seal_audit_period(self.org, start, end, actor=self.owner)
        # Drop the archive and the rows the seal covered, as a purge would, then break a survivor.
        AuditSeal.objects.filter(pk=seal.pk).update(status="purged")
        if connection.vendor == "mysql":
            self.skipTest("the DELETE guard makes the un-sanctioned trim impossible on MySQL")
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM core_auditevent WHERE organization_id=%s AND occurred_at < %s",
                           [self.org.pk.hex, self._sql(end)])
            survivor = AuditEvent.objects.order_by("occurred_at", "id").first()
            cursor.execute("UPDATE core_auditevent SET metadata=%s WHERE id=%s",
                           ['{"safe": "no"}', survivor.pk.hex])
        self.assertIn(str(survivor.pk), verify_audit_chain(self.org))

    def test_the_python_layer_refuses_a_purge_with_no_seal_at_all(self):
        from django.db import connection
        from .services import verify_audit_chain
        self.history()
        self.assertEqual([], verify_audit_chain(self.org))
        if connection.vendor == "mysql":
            with self.assertRaises(Exception):
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM core_auditevent WHERE organization_id=%s", [self.org.pk.hex])
            self.assertTrue(AuditEvent.objects.exists(), "the trigger is the last word on MySQL")
        else:
            # No trigger on sqlite, so the model layer is what stands between a query and a shortened
            # chain — and it refuses both the queryset and the instance path.
            with self.assertRaises(ValueError):
                AuditEvent.objects.filter(organization=self.org).delete()
            with self.assertRaises(ValueError):
                AuditEvent.objects.first().delete()

    # -- the surfaces --

    def test_an_owner_seals_and_trims_from_the_audit_page_and_an_auditor_may_not(self):
        from .services import audit_retention_state
        self.history()
        self.client.force_login(self.auditor)
        self.assertEqual(403, self.client.post(reverse("audit_seal_run"), {"action": "seal_purge"}).status_code)
        self.client.force_login(self.officer)
        self.assertEqual(403, self.client.get(reverse("audit_log")).status_code)
        self.client.force_login(self.owner)
        due = len(audit_retention_state(self.org)["due_periods"])
        self.assertGreater(due, 0)
        page = self.client.get(reverse("audit_log")).content.decode()
        self.assertIn("Retention", page)
        self.assertIn(f"Seal and trim {due} period", page)
        answer = self.client.post(reverse("audit_seal_run"), {"action": "seal_purge"}, follow=True)
        self.assertEqual(200, answer.status_code)
        self.assertEqual(due, AuditSeal.objects.filter(status="purged").count())
        self.assertIn("re-derived", self.client.post(reverse("audit_seal_verify",
            args=[AuditSeal.objects.first().pk]), follow=True).content.decode())

    def test_the_archive_download_is_narrower_than_the_export_because_it_is_unredacted(self):
        from .services import audit_periods_due, seal_audit_period
        self.history()
        seal = seal_audit_period(self.org, *audit_periods_due(self.org)[0][0], actor=self.owner)
        self.client.force_login(self.auditor)
        self.assertEqual(403, self.client.get(reverse("audit_seal_download", args=[seal.pk])).status_code)
        self.client.force_login(self.owner)
        answer = self.client.get(reverse("audit_seal_download", args=[seal.pk]))
        self.assertEqual(200, answer.status_code)
        self.assertEqual("application/x-ndjson", answer["Content-Type"])
        self.assertIn("attachment;", answer["Content-Disposition"])
        self.assertEqual("private, no-store", answer["Cache-Control"])
        body = b"".join(answer.streaming_content).decode()
        self.assertEqual(seal.event_count + 1, len([line for line in body.splitlines() if line.strip()]),
                         "one header line plus the events, and nothing else")
        self.assertTrue(AuditEvent.objects.filter(action="audit.seal_downloaded").exists())

    def test_the_command_dry_runs_then_enforces_and_leaves_a_small_window_alone(self):
        from io import StringIO
        from django.core.management import call_command
        from .services import audit_periods_due
        self.history()
        due = len(audit_periods_due(self.org)[0])
        self.assertEqual(3, due)
        out = StringIO()
        call_command("seal_audit_history", "--dry-run", stdout=out)
        self.assertIn("due", out.getvalue())
        self.assertEqual(0, AuditSeal.objects.count(), "--dry-run must write nothing")
        call_command("seal_audit_history", "--no-purge", stdout=StringIO())
        self.assertEqual(due, AuditSeal.objects.count())
        self.assertEqual(0, AuditSeal.objects.filter(status="purged").count())
        # Sealing is idempotent: a second pass finds nothing new, because the walk continues from the
        # last seal's own end rather than starting again at the beginning of the table.
        self.assertEqual([], audit_periods_due(self.org)[0])
        call_command("seal_audit_history", stdout=StringIO())
        self.assertEqual(due, AuditSeal.objects.filter(status="purged").count())
        other = Organization.objects.create(legal_name="Short Co", display_name="Short", slug="short-co",
                                            audit_retention_days=14)
        out = StringIO()
        call_command("seal_audit_history", "--organization", "short-co", stdout=out)
        self.assertIn("floor", out.getvalue())
        self.assertEqual(0, AuditSeal.objects.filter(organization=other).count())

    def test_retention_state_tells_the_truth_about_what_was_removed(self):
        from .services import audit_periods_due, audit_retention_state, purge_sealed_audit, seal_audit_period
        self.history()
        total = AuditEvent.objects.count()
        seal = seal_audit_period(self.org, *audit_periods_due(self.org)[0][0], actor=self.owner)
        purge_sealed_audit(seal, self.owner)
        state = audit_retention_state(self.org)
        # Sealing and purging are themselves audited, and both events land after the boundary, so they are
        # live rows the trim never touches: the count is the remainder plus the two acts of retention.
        self.assertEqual(total - seal.event_count + 2, state["live_events"])
        self.assertEqual(seal.event_count, state["sealed_events"])
        self.assertEqual([], state["chain_errors"])
        self.assertIsNotNone(state["oldest_live"])
        self.assertLessEqual(seal.period_end, state["oldest_live"])


class MessageConsentAndDeliveryLifecycleTest(TestCase):
    """NTF-4: consent that belongs to a number, and the callbacks that end a send path.

    Aimed at the two ways this feature is usually fake. A consent check that reads a boolean off the
    person breaks the day a carrier reissues a phone; a webhook that only records what a provider said
    satisfies "retained" while leaving "processed" unbuilt. So the assertions are about *decisions* —
    what may be sent to which destination after which event — and several are "nothing happened", which
    is the only shape that catches an over-broad gate.

    Nothing here ever reaches a provider: the allowed path is asserted through the decision function,
    because the alternative is a test suite that dials Twilio.
    """

    PHONE = "+1 (214) 555-0142"          # what a roster sheet looks like
    PLAIN = "+12145550142"               # the same number, one spelling, in the ledger
    OTHER = "+12145559999"
    STRANGER = "+12145557777"
    ADDRESS = "ana.del@example.com"

    def setUp(self):
        from datetime import timedelta
        from django.utils import timezone
        User = get_user_model()
        self.now = timezone.now().replace(microsecond=0, second=0)
        self.timedelta = timedelta
        self.owner = User.objects.create_user(username="consent-owner@example.com", password="pw-consent-owner")
        self.worker = User.objects.create_user(username="consent-ana@example.com", password="pw-consent-ana")
        self.auditor = User.objects.create_user(username="consent-auditor@example.com", password="pw-consent-auditor")
        self.org = Organization.objects.create(legal_name="Gate Keepers LLC", display_name="Gates",
                                               slug="gates-consent")
        for user, role in ((self.owner, Membership.Role.OWNER), (self.worker, Membership.Role.OFFICER),
                           (self.auditor, Membership.Role.AUDITOR)):
            Membership.objects.create(user=user, organization=self.org, role=role)
        self.ana = Person.objects.create(organization=self.org, first_name="Ana", last_name="Del",
                                        user=self.worker, mobile_phone=self.PHONE, email=self.ADDRESS,
                                        status=Person.Status.ACTIVE)

    def grant(self, person=None, destination=None):
        from .services import record_consent
        return record_consent(organization=self.org, person=person if person is not None else self.ana,
            destination=destination or self.PHONE, state=MessageConsent.State.GRANTED)

    def notice(self, channel, event_type="punch.exception", destination=None):
        return Notification.objects.create(organization=self.org, recipient=self.worker, channel=channel,
            destination=destination if destination is not None else (self.ADDRESS if channel == "email" else ""),
            event_type=event_type, subject="Notice", body="Body", mandatory=event_type.startswith(
                ("punch.", "payroll.", "credential.", "document.", "audit.", "message.", "import.", "retention.", "organization.")),
            deduplication_key=f"{event_type}:{channel}:{uuid.uuid4().hex[:8]}")

    def reason(self, notification):
        from .services import send_block_reason
        return send_block_reason(notification)

    # -- the ledger --

    def test_one_number_has_one_spelling_and_a_silly_string_has_no_consent_at_all(self):
        from .services import normalize_destination
        self.assertEqual(self.PLAIN, normalize_destination(self.PHONE))
        self.assertEqual(self.PLAIN, normalize_destination("1 (214) 555-0142"))
        self.assertEqual(self.PLAIN, normalize_destination("+12145550142"))
        self.assertEqual("", normalize_destination("555-014"))
        self.assertEqual("", normalize_destination("not a number"))
        self.assertEqual(self.ADDRESS, normalize_destination("Ana.Del@example.com", MessageConsent.Channel.EMAIL))
        self.assertEqual(self.PLAIN, self.grant().destination)

    def test_consent_belongs_to_the_number_so_a_new_number_is_a_new_question(self):
        from .services import sms_opted_in
        self.grant()
        self.assertTrue(sms_opted_in(self.org, self.PHONE))
        self.assertFalse(sms_opted_in(self.org, self.OTHER),
                         "an opt-in must not follow a person onto a phone nobody asked about")
        self.assertIsNone(self.reason(self.notice("sms")), "the number on file is consented, so this may go")
        self.ana.mobile_phone = self.OTHER
        self.ana.save(update_fields=["mobile_phone"])
        self.assertIn("No text-message consent", self.reason(self.notice("sms")))

    def test_an_opt_out_is_appended_beside_its_opt_in_rather_than_over_it(self):
        from .services import record_consent
        self.grant()
        record_consent(organization=self.org, person=self.ana, destination=self.PHONE,
            state=MessageConsent.State.REVOKED)
        rows = list(MessageConsent.objects.filter(organization=self.org).order_by("decided_at", "created_at"))
        self.assertEqual(["granted", "revoked"], [row.state for row in rows])
        self.assertTrue(all(row.wording for row in rows),
                        "consent to a sentence nobody can reproduce is consent to nothing")
        self.assertIn("Gates", rows[0].wording, "the wording names the company that will be texting")
        self.assertIn("STOP", rows[0].wording, "the opt-out has to be in the text they agreed to")

    def test_the_audit_chain_sees_four_digits_and_the_ledger_sees_the_number(self):
        self.grant()
        event = AuditEvent.objects.filter(action="message.consent_granted").latest("occurred_at")
        self.assertNotIn("5550142", json.dumps(event.metadata),
                         "the chain is append-only and survives retention, so it must not carry contact details")
        self.assertEqual("••••0142", event.metadata["destination"])
        self.assertEqual(self.PLAIN, MessageConsent.objects.get(pk=event.metadata["ledger"]).destination)

    def test_a_grant_clears_an_unsubscribe_block_and_cannot_clear_the_other_kinds(self):
        from .services import active_suppression
        for kind, clearable in ((Suppression.Kind.UNSUBSCRIBE, True), (Suppression.Kind.HARD_BOUNCE, False),
                                (Suppression.Kind.COMPLAINT, False), (Suppression.Kind.UNKNOWN_NUMBER, False)):
            with self.subTest(kind=kind):
                Suppression.objects.filter(organization=self.org, destination=self.PLAIN).delete()
                Suppression.objects.create(organization=self.org, destination=self.PLAIN, channel="sms", kind=kind)
                self.grant()
                blocked = active_suppression(self.org, self.PHONE)
                if clearable:
                    self.assertIsNone(blocked, "a block the person made themselves is theirs to lift")
                else:
                    self.assertIsNotNone(blocked, f"{kind} is a fact about the number, not a preference")
                    self.assertEqual(kind, blocked.kind)

    # -- the send path --

    def test_nothing_is_texted_without_a_grant_and_a_refusal_costs_no_retry(self):
        from .services import deliver_notification, queue_notice
        item = self.notice("sms")
        result = deliver_notification(item)
        self.assertEqual(Notification.Status.BLOCKED, result.status)
        self.assertEqual(0, result.attempts,
                         "a notice never sent to a carrier spent no attempt, and attempts is what ends retries")
        self.assertIn("No text-message consent", result.last_error)
        self.assertTrue(AuditEvent.objects.filter(action="message.blocked").exists())
        self.grant()
        self.assertEqual(1, queue_notice(organization=self.org, recipients=[self.worker.pk],
            event_type="shift.published", subject="New post", body="x",
            dedup_key=f"cleared:{uuid.uuid4().hex[:6]}", channels=(Notification.Channel.SMS,)),
            "queueing is not where the gate lives — a notice may always be queued, and the send decides")

    def test_a_stopped_number_stays_stopped_even_for_a_required_notice(self):
        from .services import deliver_notification, record_consent
        self.grant()
        mandatory = self.notice("sms", event_type="payroll.exported")
        self.assertTrue(mandatory.mandatory)
        self.assertIsNone(self.reason(mandatory))
        record_consent(organization=self.org, person=self.ana, destination=self.PHONE,
            state=MessageConsent.State.REVOKED)
        again = self.notice("sms", event_type="payroll.exported")
        self.assertIsNotNone(self.reason(again),
                             "an opted-out number gets nothing, however important we think our message is")
        self.assertEqual(Notification.Status.BLOCKED, deliver_notification(mandatory).status)

    def test_the_email_rule_is_not_the_text_rule_because_the_two_have_different_authors(self):
        Suppression.objects.create(organization=self.org, destination=self.ADDRESS, channel="email",
            kind=Suppression.Kind.UNSUBSCRIBE, reason="Unsubscribed from routine mail")
        optional = self.notice("email", event_type="shift.published")
        required = self.notice("email", event_type="payroll.exported")
        self.assertFalse(optional.mandatory)
        self.assertIsNotNone(self.reason(optional))
        self.assertIsNone(self.reason(required),
                          "transactional mail is carved out of the unsubscribe rule; a pay notice still goes")
        Suppression.objects.create(organization=self.org, destination="dead@example.com", channel="email",
            kind=Suppression.Kind.UNKNOWN_NUMBER)
        dead = self.notice("email", event_type="payroll.exported", destination="dead@example.com")
        self.assertTrue(dead.mandatory)
        self.assertIsNotNone(self.reason(dead), "a mailbox that does not exist is not a question of wishes")
        # And the same is true of an administrator's own block, which is a decision about the tenant.
        Suppression.objects.create(organization=self.org, destination="spam@example.com", channel="email",
            kind=Suppression.Kind.MANUAL, reason="Abuse ticket")
        self.assertIsNotNone(self.reason(self.notice("email", event_type="payroll.locked",
                                                   destination="spam@example.com")))

    def test_an_in_app_notice_is_never_blocked_for_consent_reasons(self):
        self.assertIsNone(self.reason(self.notice("in_app")))
        self.assertIsNone(self.reason(self.notice("in_app", event_type="shift.published")))

    def test_a_notice_categorises_itself_by_family_when_nobody_says(self):
        from .services import event_is_mandatory, queue_notice
        for event_type, expected in (("punch.exception", True), ("payroll.locked", True),
                                     ("credential.reminder", True), ("document.acknowledgment_requested", True),
                                     ("retention.disposition_requested", True), ("import.completed", True),
                                     ("shift.published", False), ("training.reminder", False),
                                     ("timeoff.requested", False), ("membership.invitation", True)):
            with self.subTest(event_type=event_type):
                self.assertEqual(expected, event_is_mandatory(event_type))
        queue_notice(organization=self.org, recipients=[self.worker.pk], event_type="shift.published",
            subject="New post", body="x", dedup_key=f"cat-{uuid.uuid4().hex[:6]}",
            channels=(Notification.Channel.EMAIL,))
        queued = Notification.objects.get(event_type="shift.published", channel=Notification.Channel.EMAIL)
        self.assertFalse(queued.mandatory)
        queue_notice(organization=self.org, recipients=[self.worker.pk], event_type="shift.published",
            subject="Override", body="x", dedup_key=f"cat2-{uuid.uuid4().hex[:6]}", mandatory=True,
            channels=(Notification.Channel.IN_APP,))
        self.assertTrue(Notification.objects.filter(event_type="shift.published", channel="in_app",
                                                   mandatory=True).exists(),
                        "an explicit answer beats the inference, either way round")

    # -- the provider's side --

    def test_four_providers_shapes_reduce_to_the_same_three_facts(self):
        from .services import normalize_destination, normalize_provider_callback
        failed = normalize_provider_callback("twilio", params={"MessageSid": "SM1", "To": self.PHONE,
            "From": "+12105550000", "MessageStatus": "failed", "ErrorCode": "30006",
            "ErrorMsg": "Phone number is not in service"})
        self.assertEqual(1, len(failed))
        self.assertEqual(DeliveryEvent.Kind.FAILED, failed[0]["kind"])
        # The reader hands back what the provider named; the normalization that makes it one key
        # happens where the row is written, so an unmapped spelling is visible before it is absorbed.
        self.assertEqual(self.PHONE, failed[0]["destination"])
        self.assertEqual(self.PLAIN, normalize_destination(failed[0]["destination"]))
        self.assertEqual(Suppression.Kind.UNKNOWN_NUMBER, failed[0]["suppression"])
        self.assertIn("30006", failed[0]["detail"])
        sns = json.dumps({"notificationType": "Bounce", "mail": {"messageId": "m1", "destination": [self.ADDRESS],
            "timestamp": "2026-10-01T12:00:00Z"},
            "bounce": {"bounceType": "Permanent", "bouncedRecipients": [{"emailAddress": self.ADDRESS}]}})
        parsed = normalize_provider_callback("sns", payload={"Type": "Notification", "Message": sns})
        self.assertEqual(DeliveryEvent.Kind.BOUNCE, parsed[0]["kind"])
        self.assertEqual(Suppression.Kind.HARD_BOUNCE, parsed[0]["suppression"])
        self.assertEqual(self.ADDRESS, parsed[0]["destination"])
        self.assertIsNotNone(parsed[0]["occurred_at"], "the provider's own time is kept, not our receipt time")
        mailjet = normalize_provider_callback("mailjet", payload=[{"event": "spamcomplained",
            "email": self.ADDRESS, "id": "9"}])
        self.assertEqual(DeliveryEvent.Kind.COMPLAINT, mailjet[0]["kind"])
        self.assertEqual(Suppression.Kind.COMPLAINT, mailjet[0]["suppression"])
        postmark = normalize_provider_callback("postmark", payload={"RecordType": "Bounce",
            "Email": self.ADDRESS, "BounceType": "HardBounce", "MessageID": "p1"})
        self.assertEqual(DeliveryEvent.Kind.BOUNCE, postmark[0]["kind"])
        self.assertEqual("p1", postmark[0]["reference"])
        # An event nobody recognizes is retained as a status and changes nothing: dropping it would
        # lose a fact the provider reported, and mapping it to a guess would block somebody's officer
        # because a payload shape changed on the provider's side.
        mystery = normalize_provider_callback("mailjet", payload=[{"event": "mystery", "email": self.ADDRESS}])
        self.assertEqual(1, len(mystery))
        self.assertEqual(DeliveryEvent.Kind.STATUS, mystery[0]["kind"])
        self.assertNotIn("suppression", mystery[0], "unrecognized means unacted, not silently guessed")
        self.assertEqual([], normalize_provider_callback("postmark", payload={"RecordType": "Tomorrow"}),
                         "a record naming no destination cannot be filed against anybody")
        unknown = normalize_provider_callback("twilio", params={"MessageStatus": "who-knows", "To": self.PHONE})
        self.assertEqual(DeliveryEvent.Kind.STATUS, unknown[0]["kind"])
        self.assertNotIn("suppression", unknown[0])

    def test_a_callback_changes_the_next_send_and_leaves_the_notice_that_bounced_alone(self):
        from .services import ingest_provider_events, normalize_provider_callback
        self.grant(destination=self.OTHER)
        before = self.notice("sms", event_type="payroll.locked", destination=self.OTHER)
        self.assertIsNone(self.reason(before), "sanity: consented, so it was sendable before the callback")
        events = normalize_provider_callback("twilio", params={"MessageSid": "SM9", "To": self.OTHER,
            "From": "+12105550000", "MessageStatus": "failed", "ErrorCode": "30006"})
        stored = ingest_provider_events(self.org, "twilio", events, verified=True, raw={"a": 1})
        self.assertEqual(1, len(stored))
        self.assertTrue(stored[0].applied)
        self.assertTrue(stored[0].verified)
        self.assertEqual(Suppression.Kind.UNKNOWN_NUMBER,
                         Suppression.objects.get(organization=self.org, destination=self.OTHER).kind)
        after = self.notice("sms", event_type="payroll.locked", destination=self.OTHER)
        self.assertIsNotNone(self.reason(after), "the number is now blocked for texts, required notice or not")
        before.refresh_from_db()
        self.assertEqual(Notification.Status.QUEUED, before.status,
                         "a bounce never rewrites the history of the message that bounced")
        self.assertEqual(1, DeliveryEvent.objects.filter(organization=self.org, provider="twilio").count())

    def test_a_reversal_in_the_same_clock_tick_still_wins(self):
        from unittest import mock
        from django.utils import timezone
        from .services import record_consent, sms_opted_in
        frozen = timezone.now()
        with mock.patch("django.utils.timezone.now", return_value=frozen):
            for _ in range(5):
                record_consent(organization=self.org, destination=self.PHONE, state=MessageConsent.State.GRANTED)
                record_consent(organization=self.org, destination=self.PHONE, state=MessageConsent.State.REVOKED)
                self.assertFalse(sms_opted_in(self.org, self.PHONE))

    def test_stop_works_from_the_number_itself_and_answers_on_the_same_channel(self):
        from .services import active_suppression, deliver_notification, handle_inbound_message, record_consent, sms_opted_in
        self.grant()
        reply = handle_inbound_message(self.org, self.PHONE, "STOP")
        self.assertIn("will not receive more texts", reply)
        self.assertFalse(sms_opted_in(self.org, self.PHONE))
        self.assertEqual(Suppression.Kind.UNSUBSCRIBE,
                         Suppression.objects.get(organization=self.org, destination=self.PLAIN).kind)
        self.assertEqual(Notification.Status.BLOCKED, deliver_notification(self.notice("sms")).status)
        inbound = DeliveryEvent.objects.filter(kind=DeliveryEvent.Kind.INBOUND).latest("created_at")
        self.assertTrue(inbound.applied)
        self.assertIn("START", inbound.raw["reply"],
                      "a stop that does not say how to come back is a one-way door on a shared handset")
        # A reply from a stranger is not consent for them to be texted back.
        cold = handle_inbound_message(self.org, self.STRANGER, "START")
        self.assertIn("supervisor", cold)
        self.assertFalse(MessageConsent.objects.filter(destination=self.STRANGER).exists())
        # The person who opted out is the only one who can put it back, and doing so lifts the block
        # their own STOP created — nothing else on the number's record.
        warm = handle_inbound_message(self.org, self.PHONE, "START")
        self.assertIn("again", warm)
        self.assertTrue(sms_opted_in(self.org, self.PHONE))
        self.assertIsNone(active_suppression(self.org, self.PHONE))
        self.assertIsNone(self.reason(self.notice("sms")))
        record_consent(organization=self.org, person=self.ana, destination=self.PHONE,
            state=MessageConsent.State.REVOKED)
        self.assertIn("STOP", handle_inbound_message(self.org, self.PHONE, "help").upper())

    def test_the_callback_endpoint_addresses_one_company_and_no_one_else(self):
        from .services import organization_webhook_token, twilio_signature
        token = organization_webhook_token(self.org)
        url = reverse("provider_callback", args=["twilio", token])
        self.assertEqual(404, self.client.post(
            reverse("provider_callback", args=["twilio", "x" * len(token)]),
            {"MessageStatus": "delivered", "To": self.PHONE}).status_code)
        self.assertEqual(404, self.client.post(reverse("provider_callback", args=["sendgrid", token]),
            {"MessageStatus": "delivered"}).status_code, "an unapproved provider is not a supported shape")
        answer = self.client.post(url, {"MessageStatus": "delivered", "To": self.PHONE, "MessageSid": "SM1"})
        self.assertEqual(200, answer.status_code)
        first = DeliveryEvent.objects.get(organization=self.org)
        self.assertFalse(first.verified, "no signing secret is configured, so this was addressed, not proven")
        with self.settings(TWILIO_AUTH_TOKEN="test-auth-token"):
            forged = self.client.post(url, {"MessageStatus": "delivered", "To": self.OTHER})
            self.assertEqual(403, forged.status_code)
            self.assertEqual(1, DeliveryEvent.objects.filter(organization=self.org).count(),
                             "a refused callback must not be able to write state")
            self.assertTrue(AuditEvent.objects.filter(action="message.callback_refused").exists())
            signed = twilio_signature("http://testserver" + url,
                {"MessageStatus": "delivered", "To": self.OTHER}, "test-auth-token")
            honest = self.client.post(url, {"MessageStatus": "delivered", "To": self.OTHER},
                HTTP_X_TWILIO_SIGNATURE=signed)
            self.assertEqual(200, honest.status_code)
            self.assertTrue(DeliveryEvent.objects.filter(organization=self.org).latest("created_at").verified)

    def test_an_sns_subscription_is_handed_to_a_human_rather_than_fetched_at(self):
        from .services import organization_webhook_token
        token = organization_webhook_token(self.org)
        answer = self.client.post(reverse("provider_callback", args=["sns", token]), data=json.dumps({
            "Type": "SubscriptionConfirmation", "MessageId": "m0",
            "TopicArn": "arn:aws:sns:us-east-1:123456789012:tscm",
            "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&token=t"}),
            content_type="application/json")
        self.assertEqual(202, answer.status_code)
        event = DeliveryEvent.objects.get(kind=DeliveryEvent.Kind.STATUS)
        self.assertFalse(event.applied,
                         "the confirmation URL arrives inside the payload; following it is an SSRF")
        self.assertIn("pending", event.detail)
        self.assertEqual("sns.us-east-1.amazonaws.com", event.raw["subscribe_url_host"],
                         "the host is kept so a human can judge it, and the URL is truncated")
        self.assertEqual(0, MessageConsent.objects.filter(organization=self.org).count())

    def test_a_rotated_address_retires_the_old_one(self):
        from .services import organization_webhook_token, rotate_webhook_token
        old = organization_webhook_token(self.org)
        self.client.force_login(self.owner)
        self.assertEqual(405, self.client.get(reverse("messaging_rotate_token")).status_code)
        self.client.post(reverse("messaging_rotate_token"))
        self.org.refresh_from_db()
        self.assertNotEqual(old, self.org.webhook_token)
        self.assertEqual(404, self.client.post(reverse("provider_callback", args=["twilio", old]),
            {"MessageStatus": "delivered", "To": self.PHONE}).status_code)
        self.assertEqual(200, self.client.post(reverse("provider_callback", args=["twilio", self.org.webhook_token]),
            {"MessageStatus": "delivered", "To": self.PHONE}).status_code)
        self.assertTrue(AuditEvent.objects.filter(action="organization.webhook_rotated").exists())

    # -- the surfaces --

    def test_accepting_an_invitation_can_carry_the_opt_in_and_a_blank_one_carries_nothing(self):
        invitation, token = MembershipInvitation.issue(organization=self.org, email="new@exam.com",
            role=Membership.Role.OFFICER, invited_by=self.owner,
            expires_at=self.now + self.timedelta(days=7),
            person=Person.objects.create(organization=self.org, first_name="New", last_name="Officer",
                                        status=Person.Status.ONBOARDING))
        self.client.post(reverse("invitation_accept", args=[token]), {
            "first_name": "New", "last_name": "Officer", "password": "a sufficiently long passphrase",
            "password_confirmation": "a sufficiently long passphrase", "mobile_phone": self.OTHER,
            "text_alerts": "on"})
        rows = MessageConsent.objects.filter(organization=self.org)
        self.assertEqual(1, rows.count())
        self.assertEqual(MessageConsent.Source.SIGNUP, rows.first().source)
        self.assertEqual(self.OTHER, rows.first().destination)
        self.assertEqual("invitation_accept", rows.first().evidence["surface"])
        self.assertTrue(rows.first().evidence.get("ip"))
        self.assertEqual(self.OTHER, rows.first().person.mobile_phone,
                         "the number and the consent for it are stored together or not at all")
        quiet, quiet_token = MembershipInvitation.issue(organization=self.org, email="quiet@exam.com",
            role=Membership.Role.OFFICER, invited_by=self.owner, expires_at=self.now + self.timedelta(days=7),
            person=Person.objects.create(organization=self.org, first_name="Quiet", last_name="Person",
                                        status=Person.Status.ONBOARDING))
        self.client.post(reverse("invitation_accept", args=[quiet_token]), {
            "first_name": "Quiet", "last_name": "Person", "password": "another sufficiently long phrase",
            "password_confirmation": "another sufficiently long phrase", "mobile_phone": self.STRANGER})
        self.assertEqual(1, MessageConsent.objects.filter(organization=self.org).count(),
                         "silence is not a refusal, and inventing a no row would misreport the person")
        self.assertEqual(self.STRANGER, Person.objects.get(first_name="Quiet").mobile_phone)

    def test_the_prompt_is_asked_once_and_either_answer_ends_it(self):
        from .services import current_consent
        self.client.force_login(self.worker)
        self.assertIn("Text you about your posts", self.client.get(reverse("dashboard")).content.decode())
        self.client.post(reverse("text_alerts"), {"mobile_phone": self.PHONE, "choice": "no"})
        self.assertNotIn("Text you about your posts", self.client.get(reverse("dashboard")).content.decode())
        self.assertEqual(MessageConsent.State.REVOKED, current_consent(self.org, self.PHONE).state)
        self.assertEqual(MessageConsent.Source.FIRST_LOGIN, current_consent(self.org, self.PHONE).source)

    def test_the_officer_page_records_an_answer_and_the_number_it_was_about(self):
        from .services import active_suppression, sms_opted_in
        self.ana.mobile_phone = ""
        self.ana.save(update_fields=["mobile_phone"])
        self.client.force_login(self.worker)
        self.client.post(reverse("text_alerts"), {"mobile_phone": self.PHONE, "opt_in": "on", "choice": "yes"})
        self.ana.refresh_from_db()
        self.assertEqual(self.PHONE, self.ana.mobile_phone)
        self.assertTrue(sms_opted_in(self.org, self.PHONE))
        self.assertIsNone(active_suppression(self.org, self.PHONE),
                          "an opt-in must never leave a block behind — the page would then warn the "
                          "officer about the number they just opted in")
        page = self.client.get(reverse("text_alerts")).content.decode()
        self.assertIn("••••0142", page, "the ledger masks the number even on the officer's own page")
        self.assertNotIn("+12145550142", page, "a readable page is not a phone-list export")
        self.assertIn("Texts from Gates", page, "the wording shown is the wording recorded")
        self.assertIn("Opted in", page, "the officer sees their own answer, not an empty list")
        self.assertNotIn("No decisions recorded", page)
        # Change the number and the new one is a fresh question. The old answer stays on the record,
        # because it belongs to that number's history: a carrier reissuing it to someone else must be
        # able to find that the last holder opted in, and must not inherit it.
        self.client.post(reverse("text_alerts"), {"mobile_phone": self.OTHER, "opt_in": "on", "choice": "yes"})
        self.assertTrue(sms_opted_in(self.org, self.OTHER))
        self.assertTrue(sms_opted_in(self.org, self.PHONE))
        self.assertEqual(2, MessageConsent.objects.filter(organization=self.org).count())
        self.assertEqual(self.OTHER, Person.objects.get(pk=self.ana.pk).mobile_phone)
        self.assertIsNone(self.reason(self.notice("sms")), "the number on the record is the one consulted")

    def test_a_person_without_a_number_is_not_asked_and_cannot_opt_in_blindly(self):
        self.ana.mobile_phone = ""
        self.ana.save(update_fields=["mobile_phone"])
        self.client.force_login(self.worker)
        self.assertNotIn("Text you about your posts", self.client.get(reverse("dashboard")).content.decode())
        refused = self.client.post(reverse("text_alerts"), {"opt_in": "on", "mobile_phone": "", "choice": "yes"})
        self.assertEqual(200, refused.status_code)
        self.assertIn("A number is needed", refused.content.decode())
        self.assertEqual(0, MessageConsent.objects.filter(organization=self.org).count())

    def test_only_the_officer_and_the_owners_reach_the_messaging_surfaces(self):
        self.client.force_login(self.worker)
        self.assertEqual(403, self.client.get(reverse("messaging_settings")).status_code)
        self.assertEqual(200, self.client.get(reverse("text_alerts")).status_code)
        self.client.force_login(self.auditor)
        self.assertEqual(403, self.client.get(reverse("messaging_settings")).status_code,
                         "the suppression list and the callback log name numbers; an auditor does not need them")
        # An auditor is a member, so the consent page is reachable in principle — an auditor simply
        # has no personnel record, and the page says so and sends them home rather than 403-ing a
        # person who is allowed to be looking at their own settings.
        prompted = self.client.get(reverse("text_alerts"))
        self.assertEqual(302, prompted.status_code)
        self.assertEqual(reverse("dashboard"), prompted.url)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("messaging_settings"))
        self.assertEqual(200, page.status_code)
        self.assertIn("Callback address", page.content.decode())
        self.assertIn("What proves a callback", page.content.decode(),
                      "the page has to say which events were signed and which were only addressed")




class AudienceChannelRuleTest(TestCase):
    """NTF-1: which channels one audience hears one family of notices on.

    The invariant under test is not "a rule changes channels" — it is that a rule *selects* and
    NTF-4 still *decides*. A channel setting that could outrank a missing consent would be the
    regulatory failure this product was told to avoid, so the load-bearing test here is the one that
    saves a rule selecting SMS for an unconsented number and asserts the send still refuses.

    The second shape worth a test is per-recipient routing inside one notice: the officer the notice
    is about and the manager it asks something of are addressed in the same call, and a design that
    could only pick channels per notice would have to choose which of them to wrong.
    """

    PHONE = "+12145550142"

    def setUp(self):
        from uuid import uuid4
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.uid = uuid4
        self.org = Organization.objects.create(legal_name="Channels LLC", display_name="Channels", slug="channels-ntf1")
        self.other_org = Organization.objects.create(legal_name="Elsewhere LLC", display_name="Elsewhere", slug="elsewhere-ntf1")
        self.owner = User.objects.create_user(username="ntf1-owner@example.com", password="pw-ntf1-owner")
        self.officer_user = User.objects.create_user(username="ntf1-officer@example.com", password="pw-ntf1-officer")
        self.hr = User.objects.create_user(username="ntf1-hr@example.com", password="pw-ntf1-hr")
        self.stranger = User.objects.create_user(username="ntf1-stranger@example.com", password="pw-ntf1-stranger")
        for user, role in ((self.owner, Membership.Role.OWNER), (self.officer_user, Membership.Role.OFFICER),
                           (self.hr, Membership.Role.HR)):
            Membership.objects.create(user=user, organization=self.org, role=role)
        self.ana = Person.objects.create(organization=self.org, user=self.officer_user, first_name="Ana",
            last_name="Delgado", status=Person.Status.ACTIVE, mobile_phone=self.PHONE)

    def rule(self, audience, family, channels, event_type="", organization=None, active=True):
        from .models import ChannelRule
        return ChannelRule.objects.create(organization=organization or self.org, audience=audience,
            family=family, event_type=event_type, channels=list(channels), active=active, created_by=self.owner)

    def queue(self, event_type="credential.reminder", recipients=None, subjects=(), channels=None):
        from .models import Notification
        from .services import queue_notice
        return queue_notice(organization=self.org,
            recipients=set(recipients if recipients is not None else [self.officer_user.pk, self.hr.pk]),
            event_type=event_type, subject="Notice", body="Body", mandatory=False,
            dedup_key=f"{event_type}:{self.uid().hex[:8]}", subject_user_ids=set(subjects),
            channels=tuple(channels) if channels else (Notification.Channel.IN_APP, Notification.Channel.EMAIL))

    def channels_of(self, user, event_type="credential.reminder"):
        from .models import Notification
        return set(Notification.objects.filter(organization=self.org, event_type=event_type,
                                               recipient=user).values_list("channel", flat=True))

    def test_a_company_without_rules_still_gets_exactly_what_the_caller_asked_for(self):
        self.queue()
        self.assertEqual({"in_app", "email"}, self.channels_of(self.officer_user))
        self.assertEqual({"in_app", "email"}, self.channels_of(self.hr))

    def test_a_rule_moves_the_audience_it_names_and_leaves_the_others_alone(self):
        from .models import ChannelAudience
        self.rule(ChannelAudience.SUBJECT.value, "credential", ["sms"])
        self.queue(subjects=[self.officer_user.pk])
        # The officer is routed by the rule; the HR manager beside them in the same call is not.
        self.assertEqual({"in_app", "sms"}, self.channels_of(self.officer_user))
        self.assertEqual({"in_app", "email"}, self.channels_of(self.hr))

    def test_being_the_person_the_notice_is_about_outranks_being_the_manager(self):
        from .models import ChannelAudience
        self.rule(ChannelAudience.SUBJECT.value, "credential", ["sms"])
        self.rule(ChannelAudience.COMPLIANCE.value, "credential", ["email"])
        self.queue(recipients=[self.hr.pk], subjects=[self.hr.pk])
        # An HR manager with a lapsed licence of their own is being told as a person, not written to
        # in the voice of somebody chasing a colleague — which is the whole reason the subject check
        # comes before the role map rather than after it.
        self.assertEqual({"in_app", "sms"}, self.channels_of(self.hr))

    def test_a_rule_cannot_switch_the_in_app_copy_off(self):
        from .models import ChannelAudience
        self.rule(ChannelAudience.WORKER.value, "punch", [])
        self.queue(event_type="punch.exception", recipients=[self.stranger.pk])
        self.assertEqual({"in_app"}, self.channels_of(self.stranger, "punch.exception"))

    def test_an_exact_notice_beats_the_family_rule_written_above_it(self):
        from .models import ChannelAudience
        self.rule(ChannelAudience.SUBJECT.value, "credential", ["email"])
        self.rule(ChannelAudience.SUBJECT.value, "credential", ["sms"], event_type="credential.escalated")
        self.queue(subjects=[self.officer_user.pk])
        self.queue(event_type="credential.escalated", subjects=[self.officer_user.pk])
        self.assertEqual({"in_app", "email"}, self.channels_of(self.officer_user))
        self.assertEqual({"in_app", "sms"}, self.channels_of(self.officer_user, "credential.escalated"))

    def test_a_paused_rule_is_not_a_rule(self):
        from .models import ChannelAudience
        self.rule(ChannelAudience.SUBJECT.value, "credential", ["sms"], active=False)
        self.queue(subjects=[self.officer_user.pk])
        self.assertEqual({"in_app", "email"}, self.channels_of(self.officer_user))

    def test_another_company_s_rule_does_not_reach_this_company_s_notice(self):
        from .models import ChannelAudience
        self.rule(ChannelAudience.SUBJECT.value, "credential", ["sms"], organization=self.other_org)
        self.queue(subjects=[self.officer_user.pk])
        self.assertEqual({"in_app", "email"}, self.channels_of(self.officer_user))

    def test_a_recipient_with_no_membership_falls_to_the_quietest_audience(self):
        from .models import ChannelAudience, Membership
        from .services import notification_audience
        self.assertEqual(ChannelAudience.WORKER.value, notification_audience(None, is_subject=False))
        self.assertEqual(ChannelAudience.PAYROLL.value, notification_audience(Membership.Role.PAYROLL, False))
        self.assertEqual(ChannelAudience.SUBJECT.value, notification_audience(Membership.Role.HR, True))

    def test_a_rule_that_chooses_text_still_cannot_outrank_the_consent_ledger(self):
        from .models import ChannelAudience, Notification
        from .services import deliver_notification, send_block_reason
        self.rule(ChannelAudience.SUBJECT.value, "credential", ["sms"])
        self.queue(subjects=[self.officer_user.pk])
        row = Notification.objects.get(recipient=self.officer_user, channel=Notification.Channel.SMS)
        self.assertIsNotNone(send_block_reason(row), "the rule queued the message; the ledger still refuses it")
        delivered = deliver_notification(row)
        self.assertEqual(Notification.Status.BLOCKED, delivered.status)
        self.assertEqual(0, delivered.attempts,
                         "a message never offered to a carrier is not a delivery failure and spends no retry")

    def test_a_granted_number_is_the_case_the_rule_exists_to_reach(self):
        from .models import ChannelAudience, Notification
        from .services import send_block_reason
        self.rule(ChannelAudience.SUBJECT.value, "credential", ["sms"])
        self.grant()
        self.queue(subjects=[self.officer_user.pk])
        row = Notification.objects.get(recipient=self.officer_user, channel=Notification.Channel.SMS)
        self.assertIsNone(send_block_reason(row))

    def grant(self, state=None):
        from .models import MessageConsent
        from .services import record_consent
        return record_consent(organization=self.org, person=self.ana, destination=self.PHONE,
            state=state or MessageConsent.State.GRANTED)

    def test_the_reach_count_moves_with_the_ledger_and_not_with_the_roster(self):
        from .models import ChannelAudience, MessageConsent, Suppression
        from .services import audience_reach, set_suppression
        reach = lambda: audience_reach(self.org, ChannelAudience.SUBJECT.value, Notification.Channel.SMS)
        self.assertEqual(0, reach())
        self.grant()
        self.assertEqual(1, reach())
        self.grant(state=MessageConsent.State.REVOKED)
        self.assertEqual(0, reach(), "a revocation is the newest answer, not a second opinion beside it")
        self.grant()
        set_suppression(organization=self.org, destination=self.PHONE, kind=Suppression.Kind.UNSUBSCRIBE)
        self.assertEqual(0, reach(), "opted in and blocked is not reachable, and the page must say so")

    def test_a_second_rule_for_the_same_audience_and_scope_is_refused_before_it_is_written(self):
        from .forms import ChannelRuleForm
        from .models import ChannelAudience, ChannelRule
        self.rule(ChannelAudience.SUBJECT.value, "credential", ["sms"])
        form = ChannelRuleForm(data={"audience": ChannelAudience.SUBJECT.value, "family": "credential",
            "event_type": "", "channels": ["email"], "active": "on"}, instance=ChannelRule(organization=self.org))
        self.assertFalse(form.is_valid())
        self.assertIn("family", form.errors)

    def test_a_rule_cannot_choose_the_channel_that_always_arrives_or_a_notice_outside_its_family(self):
        from django.core.exceptions import ValidationError
        from .models import ChannelRule
        for fields, fragment in (({"audience": "subject", "family": "credential", "channels": ["in_app"]}, "In-app"),
                                 ({"audience": "subject", "family": "payroll", "event_type": "credential.reminder",
                                   "channels": ["sms"]}, "not part of the payroll family")):
            rule = ChannelRule(organization=self.org, **fields)
            with self.assertRaises(ValidationError) as refused:
                rule.clean()
            self.assertIn(fragment, str(refused.exception))

    def test_an_owner_adds_and_removes_a_rule_from_the_messaging_page_and_both_are_audited(self):
        from .models import AuditEvent, ChannelRule
        self.client.force_login(self.owner)
        response = self.client.post(reverse("messaging_rule_add"),
            data={"audience": "subject", "family": "credential", "event_type": "", "channels": ["sms"], "active": "on"})
        self.assertEqual(302, response.status_code)
        rule = ChannelRule.objects.get(organization=self.org)
        self.assertEqual("subject", rule.audience)
        self.assertEqual(self.owner, rule.created_by, "who decided officers get texted is a question the row answers")
        self.assertTrue(AuditEvent.objects.filter(organization=self.org, action="message.rule_updated",
                                                  target_id=str(rule.pk)).exists())
        self.client.post(reverse("messaging_rule_remove", args=[rule.pk]))
        self.assertFalse(ChannelRule.objects.filter(organization=self.org).exists())
        self.assertTrue(AuditEvent.objects.filter(organization=self.org, action="message.rule_removed").exists())

    def test_an_officer_cannot_write_the_company_s_channel_rules(self):
        from .models import ChannelRule
        self.client.force_login(self.officer_user)
        response = self.client.post(reverse("messaging_rule_add"),
            data={"audience": "subject", "family": "credential", "channels": ["sms"], "active": "on"})
        self.assertEqual(403, response.status_code)
        self.assertFalse(ChannelRule.objects.filter(organization=self.org).exists())


class ReminderEscalationTest(TestCase):
    """NTF-2: a rung the ladder reaches with nothing recorded goes to somebody new.

    The escalation is measured by the state of the record, not by whether the earlier notice was
    opened — `Credential.expires_on` cannot be satisfied by clicking. Most of these tests are
    therefore about who is *not* told twice: an owner already in the reminder audience must not get a
    second copy, and an escalation that widens the audience onto people who were always in it is a
    duplicate with a new label.
    """

    def setUp(self):
        from datetime import timedelta
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from .models import CredentialType
        User = get_user_model()
        self.timedelta, timezone_local = timedelta, timezone
        self.today = timezone.localdate()
        self.org = Organization.objects.create(legal_name="Ladder LLC", display_name="Ladder", slug="ladder-ntf2")
        self.owner = User.objects.create_user(username="ntf2-owner@example.com", password="pw-ntf2-owner")
        self.hr = User.objects.create_user(username="ntf2-hr@example.com", password="pw-ntf2-hr")
        self.dispatch = User.objects.create_user(username="ntf2-dispatch@example.com", password="pw-ntf2-dispatch")
        self.payroll = User.objects.create_user(username="ntf2-payroll@example.com", password="pw-ntf2-payroll")
        self.officer_user = User.objects.create_user(username="ntf2-officer@example.com", password="pw-ntf2-officer")
        for user, role in ((self.owner, Membership.Role.OWNER), (self.hr, Membership.Role.HR),
                           (self.dispatch, Membership.Role.SCHEDULER), (self.payroll, Membership.Role.PAYROLL),
                           (self.officer_user, Membership.Role.OFFICER)):
            Membership.objects.create(user=user, organization=self.org, role=role)
        self.ana = Person.objects.create(organization=self.org, user=self.officer_user, first_name="Ana",
            last_name="Delgado", status=Person.Status.ACTIVE)
        self.credential_type = CredentialType.objects.create(organization=self.org, name="PSD Registration",
            code="psd", warning_days=90, reminder_days_before=[90, 30], evidence_required=True)

    def hold(self, days):
        from .models import Credential
        return Credential.objects.create(organization=self.org, person=self.ana,
            credential_type=self.credential_type, status=Credential.Status.ACTIVE,
            expires_on=self.today + self.timedelta(days=days), number="R-1")

    def escalate_to(self, *roles):
        self.credential_type.escalate_after_days = 30
        self.credential_type.escalate_to = [role.value for role in roles]
        self.credential_type.save()

    def rows(self, event_type):
        return Notification.objects.filter(organization=self.org, event_type=event_type)

    def test_a_missed_rung_reaches_the_escalation_role_and_nobody_else(self):
        from .services import queue_compliance_reminders
        self.escalate_to(Membership.Role.PAYROLL)
        self.hold(30)
        queue_compliance_reminders(self.today)
        escalated = self.rows("credential.escalated")
        self.assertTrue(escalated.exists(), "the escalation fired for a role the ladder never addressed")
        self.assertEqual({self.payroll.pk}, set(row.recipient_id for row in escalated))
        reminders = set(self.rows("credential.reminder").values_list("recipient_id", flat=True))
        self.assertIn(self.owner.pk, reminders)
        self.assertNotIn(self.payroll.pk, reminders, "the escalated role hears the news once, as an escalation")

    def test_a_manager_who_already_stands_in_the_audience_is_not_told_the_same_thing_twice(self):
        from .services import manager_recipients_by_person, queue_compliance_reminders
        self.assertIn(self.dispatch.pk, set(manager_recipients_by_person(self.org).get(self.ana.pk, ())),
                      "a scheduler with no scopes manages everybody, which is why escalating to one is a no-op")
        self.escalate_to(Membership.Role.SCHEDULER)
        self.hold(30)
        queue_compliance_reminders(self.today)
        self.assertFalse(self.rows("credential.escalated").exists(),
                         "they were told in the first pass; a second row is a duplicate wearing a new label")
        self.assertIn(self.dispatch.pk, set(self.rows("credential.reminder").values_list("recipient_id", flat=True)))

    def test_the_escalated_notice_names_the_rungs_that_went_unanswered(self):
        from .services import queue_compliance_reminders
        self.escalate_to(Membership.Role.PAYROLL)
        self.hold(30)
        queue_compliance_reminders(self.today)
        row = self.rows("credential.escalated").first()
        self.assertIsNotNone(row, "an escalation that never arrived cannot name the rungs it missed")
        self.assertIn("90", row.body)
        self.assertIn("no renewal has been recorded", row.body)

    def test_an_escalated_notice_is_a_required_operational_message(self):
        from .services import queue_compliance_reminders
        self.escalate_to(Membership.Role.PAYROLL)
        self.hold(30)
        queue_compliance_reminders(self.today)
        escalated = self.rows("credential.escalated")
        self.assertTrue(escalated.exists())
        self.assertTrue(all(row.mandatory for row in escalated))

    def test_renewing_the_credential_ends_the_ladder_before_it_escalates(self):
        from .services import queue_compliance_reminders
        self.escalate_to(Membership.Role.PAYROLL)
        credential = self.hold(30)
        queue_compliance_reminders(self.today)
        self.assertEqual(2, self.rows("credential.escalated").count())
        credential.expires_on = self.today + self.timedelta(days=400)
        credential.save()
        queue_compliance_reminders(self.today)
        self.assertEqual(2, self.rows("credential.escalated").count(),
                         "a renewal after the fact does not erase the escalation, but nothing new fires")

    def test_a_requirement_with_no_escalation_configured_behaves_exactly_as_it_did(self):
        from .services import queue_compliance_reminders
        self.hold(30)
        queue_compliance_reminders(self.today)
        self.assertFalse(self.rows("credential.escalated").exists())
        self.assertTrue(self.rows("credential.reminder").exists())

    def test_an_escalation_cannot_be_asked_to_fire_before_anything_was_warned(self):
        from django.core.exceptions import ValidationError
        from .models import CredentialType
        for fields, fragment in (({"escalate_after_days": 30, "escalate_to": []}, "Choose who hears"),
                                 ({"escalate_after_days": None, "escalate_to": ["owner"]}, "the lead time it triggers at"),
                                 ({"escalate_after_days": 200, "escalate_to": ["owner"]}, "nothing can have been missed yet"),
                                 ({"escalate_after_days": 30, "escalate_to": ["ghost"]}, "Unknown role")):
            row = CredentialType(organization=self.org, name="X", code="x-" + fragment[:6].replace(" ", ""),
                warning_days=90, reminder_days_before=[90, 30], **fields)
            with self.assertRaises(ValidationError) as refused:
                row.clean()
            self.assertIn(fragment, str(refused.exception))

    def test_a_stored_role_that_no_longer_exists_is_dropped_not_queried(self):
        self.credential_type.escalate_to = ["owner", "ghost_role"]
        self.assertEqual(["owner"], self.credential_type.escalation_roles)


class LocationSpoofSignalTest(TestCase):
    """CLK-4: the three things that can be *measured* about a location claim, and their controls.

    The signal never blocks the clock, so every test here is a pair: the reading that should raise a
    review, and the ordinary reading that must not. A spoof detector with no control case is a queue
    full of false alarms, and a dispatcher who learns to distrust it has no detector at all — which is
    also why an absent accuracy reading must produce no signal rather than the worst one.

    The site deliberately carries no coordinates. If it did, the geofence rule would raise its own
    exception on these punches and every assertion below would be measuring that instead.
    """

    def setUp(self):
        from datetime import timedelta
        from uuid import uuid4
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        User = get_user_model()
        self.timedelta, self.uuid4, self.timezone = timedelta, uuid4, timezone
        self.now = timezone.now().replace(microsecond=0)
        self.owner = User.objects.create_user(username="clk4-owner@example.com", password="pw-clk4-owner")
        self.officer_user = User.objects.create_user(username="clk4-officer@example.com", password="pw-clk4-officer")
        self.org = Organization.objects.create(legal_name="Signal LLC", display_name="Signal", slug="signal-clk4")
        for user, role in ((self.owner, Membership.Role.OWNER), (self.officer_user, Membership.Role.OFFICER)):
            Membership.objects.create(user=user, organization=self.org, role=role)
        self.ana = Person.objects.create(organization=self.org, user=self.officer_user, first_name="Ana",
            last_name="Delgado", status=Person.Status.ACTIVE)
        self.contract = Client.objects.create(organization=self.org, name="Reception Group")
        self.site = Site.objects.create(organization=self.org, client=self.contract, name="Ranger Tower",
            address="1 Test St")
        self.post = Shift.objects.create(organization=self.org, site=self.site, officer=self.ana,
            starts_at=self.now - self.timedelta(hours=6), ends_at=self.now + self.timedelta(hours=8),
            status=Shift.Status.PUBLISHED)

    def clock(self, kind, occurred_at, latitude=None, longitude=None, **extra):
        from .services import record_punch
        punch, _ = record_punch(organization=self.org, person=self.ana, shift=self.post, source="pwa",
            client_event_id=self.uuid4(), kind=kind, occurred_at=occurred_at, actor=self.officer_user,
            latitude=latitude, longitude=longitude, **extra)
        return punch

    def test_an_impossible_location_is_recorded_and_sent_for_review_never_blocked(self):
        punch = self.clock(Punch.Kind.IN, self.now, 31.0, -98.0, accuracy_m=0)
        self.assertEqual(["implausible_accuracy"], punch.risk_flags)
        self.assertEqual(Punch.Review.PENDING, punch.review_status)
        self.assertIn("spoof", punch.exception_reason)
        self.assertIn("0", punch.exception_reason, "the number that made it impossible is in the sentence")

    def test_a_plausible_fix_produces_no_signal_and_the_punch_stays_accepted(self):
        punch = self.clock(Punch.Kind.IN, self.now, 31.0, -98.0, accuracy_m=14)
        self.assertEqual([], punch.risk_flags)
        self.assertEqual(Punch.Review.ACCEPTED, punch.review_status)
        self.assertEqual("", punch.exception_reason)

    def test_a_page_that_sends_no_accuracy_is_not_read_as_a_zero_accuracy(self):
        from .views import _location_claims
        claims = _location_claims({"latitude": 31.0, "longitude": -98.0, "accuracy": None, "fix_age_seconds": ""})
        self.assertIsNone(claims["accuracy_m"])
        self.assertIsNone(claims["fix_age_seconds"])
        punch = self.clock(Punch.Kind.IN, self.now, 31.0, -98.0)
        self.assertEqual([], punch.risk_flags)
        self.assertEqual(Punch.Review.ACCEPTED, punch.review_status)

    def test_a_stale_online_fix_is_a_signal_and_the_same_age_offline_is_not(self):
        stale = self.clock(Punch.Kind.IN, self.now, 31.0, -98.0, fix_age_seconds=1800)
        self.assertEqual(["stale_fix"], stale.risk_flags)
        queued = self.clock(Punch.Kind.OUT, self.now - self.timedelta(minutes=1), 31.0, -98.0,
            fix_age_seconds=1800, offline=True)
        self.assertEqual([], queued.risk_flags,
                         "an offline reading is old by design; flagging it would write up every post with no signal")

    def test_a_distance_no_vehicle_covers_between_two_punches_is_a_signal(self):
        self.clock(Punch.Kind.IN, self.now - self.timedelta(minutes=20), 31.0, -98.0)
        punch = self.clock(Punch.Kind.OUT, self.now - self.timedelta(minutes=15), 32.0, -98.0)
        self.assertEqual(["impossible_travel"], punch.risk_flags)
        self.assertIn("spoof signal", punch.exception_reason)
        self.assertIn("km/h", punch.exception_reason, "the reviewer is told the speed the claim implies")

    def test_a_legitimate_drive_between_two_posts_is_not_a_signal(self):
        self.clock(Punch.Kind.IN, self.now - self.timedelta(hours=4), 31.0, -98.0)
        punch = self.clock(Punch.Kind.OUT, self.now - self.timedelta(hours=1), 32.0, -98.0)
        self.assertEqual([], punch.risk_flags)
        self.assertEqual(Punch.Review.ACCEPTED, punch.review_status)

    def test_turning_the_switch_off_records_the_signal_without_bothering_anybody(self):
        TimePolicy.objects.create(organization=self.org, flag_spoof_risk=False)
        punch = self.clock(Punch.Kind.IN, self.now, 31.0, -98.0, accuracy_m=0)
        self.assertEqual(["implausible_accuracy"], punch.risk_flags,
                         "the measurement is a fact about the punch, not a policy opinion")
        self.assertEqual(Punch.Review.ACCEPTED, punch.review_status)
        risk = AuditEvent.objects.get(target_id=str(punch.pk), action="punch.recorded").metadata["evidence"]["location_risk"]
        self.assertFalse(risk["flagged"], "the chain says the signal was seen and deliberately not raised")

    def test_a_site_that_wants_the_review_gets_it_under_a_company_that_does_not(self):
        from .services import effective_clock_policy
        TimePolicy.objects.create(organization=self.org, flag_spoof_risk=False)
        override = TimePolicyOverride.objects.create(organization=self.org, site=self.site, flag_spoof_risk=True)
        self.assertTrue(effective_clock_policy(self.org, self.site).spoof_risk["flagged"])
        punch = self.clock(Punch.Kind.IN, self.now, 31.0, -98.0, accuracy_m=0)
        self.assertEqual(Punch.Review.PENDING, punch.review_status)
        risk = AuditEvent.objects.get(target_id=str(punch.pk), action="punch.recorded").metadata["evidence"]["location_risk"]
        self.assertEqual("site", risk["source"])
        self.assertEqual(f"{override.pk}:1", risk["version"],
                         "the stamp names the row that authorized the review, not the site it stands on")

    def test_the_new_switch_is_in_the_set_a_stored_policy_version_promises_to_hold(self):
        from .services import RULE_WATCHED
        from .views import TRISTATE_FIELDS
        self.assertIn("flag_spoof_risk", RULE_WATCHED[RuleRevision.Kind.CLOCK_POLICY])
        self.assertIn("flag_spoof_risk", RULE_WATCHED[RuleRevision.Kind.CLOCK_RULE])
        self.assertIn("flag_spoof_risk", TRISTATE_FIELDS,
                      "a nullable boolean left out of this tuple renders an explicit No as Inherit")

    def test_the_spoof_switch_keeps_inherit_no_and_yes_apart_on_the_edit_screen(self):
        from .forms import TimePolicyOverrideForm
        for posted, expected in (("", None), ("yes", True), ("no", False)):
            form = TimePolicyOverrideForm(data={"site": self.site.pk, "flag_spoof_risk": posted},
                instance=TimePolicyOverride(organization=self.org))
            self.assertTrue(form.is_valid(), form.errors)
            self.assertIs(expected, form.cleaned_data["flag_spoof_risk"])

    def test_the_verdict_labels_survive_a_code_nobody_has_seen_before(self):
        punch = self.clock(Punch.Kind.IN, self.now, 31.0, -98.0, accuracy_m=0)
        punch.risk_flags = ["implausible_accuracy", "brand_new_signal"]
        labels = punch.risk_labels
        self.assertIn("location reported as good to 2 m or less", labels)
        self.assertIn("brand_new_signal", labels, "an unnameable signal must not disappear from the row")

    def test_the_clock_endpoint_carries_the_reading_through_to_the_verdict(self):
        import json
        self.client.force_login(self.officer_user)
        response = self.client.post(reverse("punch_api"), content_type="application/json", data=json.dumps({
            "kind": "in", "occurred_at": self.now.isoformat(), "shift_id": str(self.post.pk),
            "client_event_id": str(self.uuid4()), "latitude": 31.0, "longitude": -98.0, "accuracy": 1}))
        self.assertEqual(201, response.status_code)
        punch = Punch.objects.get(pk=response.json()["id"])
        self.assertEqual(["implausible_accuracy"], punch.risk_flags)
        self.assertEqual(Punch.Review.PENDING, punch.review_status)


class ClockSelfieTest(TestCase):
    """CLK-1: a face photo at the ends of a tour, under the owner's three rulings.

    The centre of gravity here is not "a photo can be uploaded" — it is the four things that stop a
    photo being *evidence*: whose face, what kind of record, already used, and how old. Each is a
    refusal, and each refusal has a control case beside it, because a check that rejects everything is
    indistinguishable from a check that works. The second group is the disclosure rung: a biometric is
    the one record type whose reader list the owner wrote by hand.
    """

    def setUp(self):
        from datetime import timedelta
        from uuid import uuid4
        from django.contrib.auth import get_user_model
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.utils import timezone
        from .services import store_clock_selfie
        User = get_user_model()
        self.timedelta, self.timezone, self.uuid4 = timedelta, timezone, uuid4
        self.store = store_clock_selfie
        self.jpg = SimpleUploadedFile
        self.now = timezone.now().replace(microsecond=0)
        self.owner = User.objects.create_user(username="clk1-owner@example.com", password="pw-clk1-owner")
        self.officer_user = User.objects.create_user(username="clk1-officer@example.com", password="pw-clk1-officer")
        self.other_user = User.objects.create_user(username="clk1-other@example.com", password="pw-clk1-other")
        self.org = Organization.objects.create(legal_name="Faces LLC", display_name="Faces", slug="faces-clk1")
        for user, role in ((self.owner, Membership.Role.OWNER), (self.officer_user, Membership.Role.OFFICER),
                           (self.other_user, Membership.Role.OFFICER)):
            Membership.objects.create(user=user, organization=self.org, role=role)
        self.ana = Person.objects.create(organization=self.org, user=self.officer_user, first_name="Ana",
            last_name="Delgado", status=Person.Status.ACTIVE)
        self.bo = Person.objects.create(organization=self.org, user=self.other_user, first_name="Bo",
            last_name="Nunez", status=Person.Status.ACTIVE)
        self.contract = Client.objects.create(organization=self.org, name="Reception Group")
        # No coordinates, so the geofence rule never fires and every exception asserted here was
        # produced by the photo rule and nothing else.
        self.site = Site.objects.create(organization=self.org, client=self.contract, name="Ranger Tower",
            address="1 Test St")
        self.post = Shift.objects.create(organization=self.org, site=self.site, officer=self.ana,
            starts_at=self.now - self.timedelta(hours=6), ends_at=self.now + self.timedelta(hours=8),
            status=Shift.Status.PUBLISHED)
        self.policy = TimePolicy.objects.create(organization=self.org, require_selfie=True)

    def jpeg_bytes(self, geotag=False):
        """A JPEG a decoder will actually read, optionally carrying a phone's diary.

        Built with Pillow instead of a byte literal because the upload path re-encodes images to clear
        their embedded location, and a magic-correct blob that no decoder can read exercises the
        refusal — which is a different rule from the one the photo tests are about.
        """
        from fractions import Fraction
        from io import BytesIO
        from PIL import Image
        image = Image.new("RGB", (48, 32), (198, 172, 140))
        if not geotag:
            output = BytesIO()
            image.save(output, format="JPEG")
            return output.getvalue()
        exif = image.getexif()
        exif[0x010F] = "PhoneMaker"                     # Make
        exif[0x0110] = "GuardModel 12"                  # Model
        exif[0x0132] = "2026:10:04 06:58:12"            # DateTime
        gps = exif.get_ifd(0x8825)
        gps[1] = "N"                                    # GPSLatitudeRef
        gps[2] = (Fraction(32), Fraction(47), Fraction(8))
        gps[3] = "W"                                    # GPSLongitudeRef
        gps[4] = (Fraction(96), Fraction(48), Fraction(41))
        output = BytesIO()
        image.save(output, format="JPEG", exif=exif.tobytes())
        return output.getvalue()

    def frame(self, person=None, name="clock-selfie.jpg", body=None):
        # Real JPEG magic, because the validator keys on the bytes and not the name or the declared
        # type — a test that faked either would be exercising a rule the upload path does not have.
        payload = body if body is not None else self.jpeg_bytes()
        return self.store(organization=self.org, person=person or self.ana,
            upload=self.jpg(name, payload, content_type="image/jpeg"), actor=self.owner)

    def punch(self, kind=Punch.Kind.IN, occurred_at=None, **extra):
        from .services import record_punch
        punch, _ = record_punch(organization=self.org, person=self.ana, shift=self.post, source="pwa",
            client_event_id=self.uuid4(), kind=kind, occurred_at=occurred_at or self.now,
            actor=self.officer_user, **extra)
        return punch

    def recorded(self, punch):
        return AuditEvent.objects.get(target_id=str(punch.pk), action="punch.recorded").metadata

    def test_a_frame_is_owed_at_the_two_ends_of_a_tour_and_at_no_other_event(self):
        from .services import selfie_owed, effective_clock_policy
        policy = effective_clock_policy(self.org, self.site)
        self.assertTrue(selfie_owed(policy=policy, kind=Punch.Kind.IN))
        self.assertTrue(selfie_owed(policy=policy, kind=Punch.Kind.OUT))
        # A checkpoint scan is its own kind, and a break is not a punch at all — the ruling's
        # "not breaks" lands here, and there is no kind to ask about it.
        self.assertFalse(selfie_owed(policy=policy, kind=Punch.Kind.CHECKPOINT))

    def test_a_missing_frame_makes_an_exception_and_still_records_the_time(self):
        punch = self.punch()
        self.assertEqual(Punch.Review.PENDING, punch.review_status)
        self.assertIn("photo was required", punch.exception_reason)
        self.assertFalse(punch.offline, "the punch exists; only its evidence is short")

    def test_a_frame_present_satisfies_the_rule_and_is_recorded_against_the_punch(self):
        document = self.frame()
        punch = self.punch(selfie=document)
        self.assertEqual(Punch.Review.ACCEPTED, punch.review_status)
        self.assertEqual("", punch.exception_reason)
        self.assertEqual(document.pk, punch.selfie_id)

    def test_the_policy_that_asked_for_the_frame_is_stamped_beside_the_answer(self):
        document = self.frame()
        punch = self.punch(selfie=document)
        selfie = self.recorded(punch)["evidence"]["selfie"]
        self.assertTrue(selfie["attached"])
        self.assertTrue(selfie["required"])
        self.assertEqual(document.sha256, selfie["sha256"])
        self.assertEqual(f"{self.policy.pk}:{self.policy.revision}", selfie["version"],
                         "the rule is versioned like the fence and the rounding, so an edited "
                         "policy cannot retroactively change what a punch claimed")

    def test_a_station_punch_is_exempt_because_its_pin_already_said_who(self):
        punch = self.punch(evidence={"kiosk": str(self.uuid4()), "kiosk_name": "Gate", "identified_by": "pin"})
        self.assertEqual(Punch.Review.ACCEPTED, punch.review_status)
        self.assertEqual("", punch.exception_reason)
        self.assertFalse(self.recorded(punch)["evidence"]["selfie"]["required"])

    def test_a_claim_of_a_station_without_the_pin_behind_it_is_not_an_exemption(self):
        # The exemption is read from evidence the caller had to earn, not from a field it can set, so
        # a hand-built payload that only names a kiosk still owes the frame.
        punch = self.punch(evidence={"kiosk": str(self.uuid4())})
        self.assertIn("photo was required", punch.exception_reason)

    def test_another_officers_face_cannot_stand_as_this_officers_evidence(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError) as refused:
            self.punch(selfie=self.frame(person=self.bo))
        self.assertIn("not this officer", str(refused.exception))

    def test_a_record_that_is_not_a_clock_photo_cannot_be_laundered_into_one(self):
        from django.core.exceptions import ValidationError
        from .services import store_person_document
        id_scan = DocumentType.objects.create(organization=self.org, name="ID scan", code="id-scan")
        document = store_person_document(organization=self.org, person=self.ana, document_type=id_scan,
            upload=self.jpg("scan.jpg", self.jpeg_bytes(), content_type="image/jpeg"),
            actor=self.owner)
        with self.assertRaises(ValidationError) as refused:
            self.punch(selfie=document)
        self.assertIn("not a clock photo", str(refused.exception))

    def test_one_frame_cannot_evidence_both_ends_of_a_tour(self):
        from django.core.exceptions import ValidationError
        document = self.frame()
        self.punch(Punch.Kind.IN, selfie=document)
        # The one-use check runs before the freshness window, so this refuses on reuse rather than on
        # arithmetic: the second claim is deliberately a past time, because "the frame is eight hours
        # old too" would let the test pass for a reason that has nothing to do with what it names.
        with self.assertRaises(ValidationError) as refused:
            self.punch(Punch.Kind.IN, occurred_at=self.now - self.timedelta(minutes=1), selfie=document)
        self.assertIn("already been used", str(refused.exception))

    def test_a_frame_from_earlier_in_the_day_is_not_evidence_about_now(self):
        from django.core.exceptions import ValidationError
        document = self.frame()
        PersonDocument.objects.filter(pk=document.pk).update(created_at=self.now - self.timedelta(hours=3))
        document.refresh_from_db()
        with self.assertRaises(ValidationError) as refused:
            self.punch(occurred_at=self.now, selfie=document)
        self.assertIn("too far to be evidence", str(refused.exception))

    def test_an_offline_punch_refuses_a_frame_rather_than_accepting_a_replayed_one(self):
        from django.core.exceptions import ValidationError
        document = self.frame()
        with self.assertRaises(ValidationError) as refused:
            self.punch(offline=True, selfie=document)
        self.assertIn("offline", str(refused.exception))
        # And the missing frame is reported as the reason it is in review, not hidden behind it.
        queued = self.punch(offline=True)
        self.assertIn("collected offline", queued.exception_reason)

    def test_a_photo_id_from_another_company_is_not_found_rather_than_forbidden(self):
        from django.core.exceptions import ValidationError
        from .views import _selfie_claim
        elsewhere = Organization.objects.create(legal_name="Elsewhere LLC", display_name="Elsewhere",
            slug="elsewhere-clk1")
        foreign = Person.objects.create(organization=elsewhere, first_name="Far", last_name="Away",
            status=Person.Status.ACTIVE)
        document = self.store(organization=elsewhere, person=foreign,
            upload=self.jpg("clock-selfie.jpg", self.jpeg_bytes(),
                            content_type="image/jpeg"), actor=None)
        with self.assertRaises(ValidationError) as refused:
            _selfie_claim(self.org, {"selfie_document_id": str(document.pk)})
        self.assertIn("not found", str(refused.exception))

    def test_the_biometric_rung_admits_exactly_the_four_roles_the_owner_named(self):
        from .models import record_readable
        document = self.frame()
        cases = {Membership.Role.OWNER: True, Membership.Role.ADMIN: True, Membership.Role.HR: True,
                 Membership.Role.SCHEDULER: True, Membership.Role.AUDITOR: False,
                 Membership.Role.PAYROLL: False, Membership.Role.SUPERVISOR: False}
        for role, allowed in cases.items():
            self.assertEqual(allowed, record_readable(document, role, subject_person_id=self.bo.pk),
                             f"{role} should {'open' if allowed else 'not open'} a face photo "
                             "about somebody else")
        # The subject sees their own, which is what makes this rung different from `sealed`.
        self.assertTrue(record_readable(document, Membership.Role.OFFICER, subject_person_id=self.ana.pk))

    def test_widening_the_rung_for_dispatch_did_not_widen_the_claim_files_too(self):
        # The reason a fourth rung exists rather than a wider `restricted`: a claim file must not have
        # become dispatcher-readable as a side effect of this ruling.
        from .models import record_readable
        from .services import store_person_document
        claim = DocumentType.objects.create(organization=self.org, name="Workers compensation claim",
            code="claim-clk1", sensitivity=DocumentType.Sensitivity.RESTRICTED)
        document = store_person_document(organization=self.org, person=self.ana, document_type=claim,
            upload=self.jpg("claim.pdf", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n trailer\n<<>>\n%%EOF\n",
                            content_type="application/pdf"), actor=self.owner)
        self.assertFalse(record_readable(document, Membership.Role.SCHEDULER, subject_person_id=self.bo.pk))
        self.assertTrue(record_readable(document, Membership.Role.HR, subject_person_id=self.bo.pk))

    def test_a_retake_deletes_the_frame_nobody_punched_with_and_keeps_the_one_somebody_did(self):
        from django.core.exceptions import ValidationError
        first = self.frame()
        second = self.store(organization=self.org, person=self.ana,
            upload=self.jpg("clock-selfie.jpg", self.jpeg_bytes(),
                            content_type="image/jpeg"), actor=self.owner, replaces=str(first.pk))
        self.assertFalse(PersonDocument.objects.filter(pk=first.pk).exists(),
                         "an unattached face is not evidence and must not age in storage on a "
                         "retake the officer already abandoned")
        attached = self.frame()
        self.punch(selfie=attached)
        with self.assertRaises(ValidationError) as refused:
            self.store(organization=self.org, person=self.ana,
                upload=self.jpg("clock-selfie.jpg", self.jpeg_bytes(),
                                content_type="image/jpeg"), actor=self.owner, replaces=str(attached.pk))
        self.assertIn("already recorded against a punch", str(refused.exception))
        self.assertTrue(PersonDocument.objects.filter(pk=attached.pk).exists())
        self.assertNotEqual(second.pk, attached.pk)

    def test_the_seeded_selfie_type_is_a_normal_editable_record_type_at_ninety_days(self):
        from .services import ensure_clock_selfie_type
        document_type = ensure_clock_selfie_type(self.org)
        self.assertEqual(document_type.retention_days, 90)
        self.assertEqual(document_type.sensitivity, DocumentType.Sensitivity.BIOMETRIC)
        # Configurable to permanent, which is the other end of the range the owner named, and a frame
        # filed afterwards carries no retain_until at all.
        document_type.retention_days = None
        document_type.save()
        document = self.frame()
        self.assertIsNone(document.retain_until)
        self.assertEqual(PersonDocument.objects.count(),
            PersonDocument.objects.filter(document_type__code="clock_selfie").count())

    def test_a_non_picture_is_refused_before_it_is_stored(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.store(organization=self.org, person=self.ana,
                upload=self.jpg("clock-selfie.jpg", b"#!/bin/sh\necho not an image\n",
                                content_type="image/jpeg"), actor=self.owner)
        self.assertEqual(0, Punch.objects.filter(selfie__isnull=False).count())

    def test_the_upload_route_records_the_frame_and_the_audit_event_behind_it(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.force_login(self.officer_user)
        response = self.client.post(reverse("clock_selfie_upload"),
            {"selfie": SimpleUploadedFile("clock-selfie.jpg", self.jpeg_bytes(),
                                          content_type="image/jpeg")})
        self.assertEqual(201, response.status_code)
        document = PersonDocument.objects.get(pk=response.json()["document_id"])
        self.assertEqual(self.ana.pk, document.person_id)
        self.assertTrue(AuditEvent.objects.filter(action="clock.selfie_captured",
                                                  target_id=str(document.pk)).exists())

    def test_the_photo_rule_resolves_per_level_and_keeps_its_tri_state_apart_from_inherit(self):
        from .forms import TimePolicyOverrideForm
        from .services import effective_clock_policy, selfie_owed
        self.policy.require_selfie = False
        self.policy.save()
        override = TimePolicyOverride.objects.create(organization=self.org, site=self.site,
            require_selfie=True)
        policy = effective_clock_policy(self.org, self.site)
        self.assertTrue(policy.selfie["required"])
        self.assertEqual("site", policy.selfie["source"])
        self.assertTrue(selfie_owed(policy=policy, kind=Punch.Kind.IN))
        for posted, expected in (("", None), ("yes", True), ("no", False)):
            form = TimePolicyOverrideForm(data={"site": self.site.pk, "require_selfie": posted},
                instance=TimePolicyOverride(organization=self.org))
            self.assertTrue(form.is_valid(), form.errors)
            self.assertIs(expected, form.cleaned_data["require_selfie"])

    def test_the_new_switch_is_watchable_bumpable_and_editable_as_three_distinct_answers(self):
        from .services import RULE_WATCHED
        from .views import TRISTATE_FIELDS
        self.assertIn("require_selfie", RULE_WATCHED[RuleRevision.Kind.CLOCK_POLICY])
        self.assertIn("require_selfie", RULE_WATCHED[RuleRevision.Kind.CLOCK_RULE])
        self.assertIn("require_selfie", TRISTATE_FIELDS,
                      "a nullable boolean left out of this tuple renders an explicit No as Inherit")
        # And the override row reports it, because `overridden_fields()` is what the settings screen
        # prints as "this level changed that" — a field missing there is a change the page cannot show.
        self.assertIn("require_selfie", TimePolicyOverride(
            organization=self.org, require_selfie=True).overridden_fields())


class DocumentMetadataStripTest(TestCase):
    """Every image that reaches storage loses the camera's diary, and the record says so.

    The hole was structural, not accidental: only the logo path re-encoded, so a licence or a diploma
    photographed at a kitchen table carried its GPS coordinates into the personnel file, into the
    export ZIP, and into the hands of everyone the `standard` sensitivity rung admits — a list that
    includes **auditor**, a role the ladder's own text describes as often an outside accountant.
    Nobody ever decided to disclose that location; the bytes simply arrived with it.

    These tests assert on what comes **back out of storage** rather than on what the service returned,
    because the storage copy is what an export reads. Each control case keeps the assertion able to
    fail: a test that never proves the submitted file *had* a geotag passes on a build that strips
    nothing.
    """

    def setUp(self):
        import tempfile
        from django.contrib.auth import get_user_model
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from .services import store_person_document
        User = get_user_model()
        self.files = SimpleUploadedFile
        self.store_person_document = store_person_document
        self.owner = User.objects.create_user(username="strip-owner@example.com", password="pw-strip")
        self.org = Organization.objects.create(legal_name="Strips LLC", display_name="Strips",
            slug="strips-media")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.Role.OWNER)
        self.ana = Person.objects.create(organization=self.org, first_name="Ana", last_name="Rios",
            status=Person.Status.ACTIVE)
        self.licence = DocumentType.objects.create(organization=self.org, name="Driver licence",
            code="dl-strip")
        media = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(media.cleanup)

    def photo(self, *, geotag=False, orientation=None, size=(48, 32), name="licence.jpg"):
        """A real JPEG, optionally carrying what a phone writes into one."""
        from fractions import Fraction
        from io import BytesIO
        from PIL import Image
        image = Image.new("RGB", size, (196, 178, 150))
        exif = image.getexif()
        if geotag or orientation:
            if geotag:
                exif[0x010F] = "PhoneMaker"
                exif[0x0110] = "GuardModel 12"
                exif[0x0132] = "2026:10:04 06:58:12"
                gps = exif.get_ifd(0x8825)
                gps[1] = "N"
                gps[2] = (Fraction(32), Fraction(47), Fraction(8))
                gps[3] = "W"
                gps[4] = (Fraction(96), Fraction(48), Fraction(41))
            if orientation:
                exif[0x0112] = orientation
        output = BytesIO()
        image.save(output, format="JPEG", exif=exif.tobytes())
        return self.files(name, output.getvalue(), content_type="image/jpeg")

    def store(self, upload, document_type=None):
        return self.store_person_document(organization=self.org, person=self.ana,
            document_type=document_type or self.licence, upload=upload, actor=self.owner)

    def kept_bytes(self, document):
        with document.file.open("rb") as handle:
            return handle.read()

    def test_a_photographed_licence_comes_back_without_its_coordinates(self):
        from io import BytesIO
        from PIL import Image
        upload = self.photo(geotag=True)
        submitted = upload.read()
        upload.seek(0)
        # The control first: if the submitted file did not actually carry the location, every
        # assertion below would pass on a build that strips nothing.
        before = Image.open(BytesIO(submitted)).getexif()
        self.assertTrue(dict(before.get_ifd(0x8825)), "the fixture has no geotag to test with")
        self.assertEqual("PhoneMaker", before.get(0x010F))
        document = self.store(upload)
        after = Image.open(BytesIO(self.kept_bytes(document))).getexif()
        self.assertEqual({}, dict(after.get_ifd(0x8825)),
                         "a stored personnel photo still carries where it was taken")
        self.assertIsNone(after.get(0x010F), "the device that took it is not evidence about the licence")
        self.assertIsNone(after.get(0x0132), "the camera's own clock is not the record's timestamp")

    def test_the_row_describes_the_bytes_that_were_kept_not_the_ones_that_were_sent(self):
        import hashlib
        upload = self.photo(geotag=True)
        submitted = upload.read()
        upload.seek(0)
        document = self.store(upload)
        kept = self.kept_bytes(document)
        self.assertNotEqual(submitted, kept, "a geotagged file was stored untouched")
        self.assertEqual(hashlib.sha256(kept).hexdigest(), document.sha256,
                         "the digest an auditor re-derives must be the digest of what is on disk")
        self.assertEqual(len(kept), document.size)

    def test_the_container_survives_so_the_preview_still_trusts_its_own_allowlist(self):
        from .services import PREVIEW_MAGIC
        document = self.store(self.photo(geotag=True, name="diploma.jpeg"))
        kept = self.kept_bytes(document)
        self.assertEqual("image/jpeg", document.verified_type)
        self.assertTrue(any(kept.startswith(sign) for sign in PREVIEW_MAGIC["image/jpeg"]),
                        "re-encoding produced bytes that are no longer what the row claims they are")

    def test_a_pdf_is_stored_byte_for_byte_because_the_bytes_are_the_record(self):
        payload = b"%PDF-1.4\n% an inspection reads exactly this\ntrailer\n<<>>\n%%EOF\n"
        document = self.store(self.files("handbook.pdf", payload, content_type="application/pdf"))
        self.assertEqual(payload, self.kept_bytes(document),
                         "the strip re-wrote a document whose bytes are themselves the evidence")
        self.assertEqual("application/pdf", document.verified_type)

    def test_an_image_no_decoder_can_read_is_refused_and_keeps_no_row(self):
        from django.core.exceptions import ValidationError
        magic_only = self.files("odd.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 2048 + b"\xff\xd9",
                                content_type="image/jpeg")
        with self.assertRaises(ValidationError) as refused:
            self.store(magic_only)
        self.assertIn("could not be re-encoded", " ".join(refused.exception.messages))
        self.assertEqual(0, PersonDocument.objects.filter(organization=self.org).count(),
                         "an image whose metadata cannot be proved gone must not be filed anyway")

    def test_the_orientation_is_baked_into_the_pixels_instead_of_carried_as_a_tag(self):
        from io import BytesIO
        from PIL import Image
        document = self.store(self.photo(orientation=6, name="rotated.jpg"))
        stored = Image.open(BytesIO(self.kept_bytes(document)))
        self.assertEqual((32, 48), stored.size, "a sideways photo stayed sideways: the tag was dropped "
                                                "without the pixels being turned")
        self.assertIsNone(stored.getexif().get(0x0112))

    def test_the_audit_row_says_whether_the_file_arrived_without_its_diary(self):
        photo = self.store(self.photo(geotag=True))
        pdf = self.store(self.files("notice.pdf", b"%PDF-1.4\nnotice", content_type="application/pdf"))
        for document, stripped in ((photo, True), (pdf, False)):
            event = AuditEvent.objects.get(target_type="person_document", target_id=str(document.pk),
                action="document.uploaded")
            self.assertEqual(stripped, event.metadata["metadata_stripped"],
                f"{document.verified_type} is reported as the wrong kind of artefact")

    def test_a_clock_frame_is_stripped_by_the_same_control_as_a_licence(self):
        """One rule, one place. A selfie reaches storage through the same door, so it must leave with
        the same diary missing — and the frame's time and place are already in the punch."""
        from io import BytesIO
        from PIL import Image
        from .services import store_clock_selfie
        document = store_clock_selfie(organization=self.org, person=self.ana,
            upload=self.photo(geotag=True, name="clock-selfie.jpg"), actor=self.owner)
        self.assertEqual({}, dict(Image.open(BytesIO(self.kept_bytes(document))).getexif()
                                  .get_ifd(0x8825)))


class SnsSubscriptionConfirmTest(TestCase):
    """The one outbound URL fetch in the product, and everything standing between it and a socket.

    An SNS subscription stays unpublished until its `SubscribeURL` is confirmed, and that address
    arrives **inside a request body an unauthenticated caller wrote**. So confirming has to be
    possible — otherwise the SNS deployments this product documents can never receive a bounce — and
    it has to be a named human click on an address that first survives an allow-list. The hostile set
    below is why the rule reads the parsed host rather than the string: every one of those contains
    `sns.us-east-1.amazonaws.com` somewhere except where it is the authority.
    """

    GOOD = "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&TopicArn=a&Token=sekret"
    OK_BODY = ('<ConfirmSubscriptionResponse xmlns="http://sns.amazonaws.com/doc/2010-03-31/">'
               '<ConfirmSubscriptionResult><SubscriptionArn>arn:aws:sns:us-east-1:1:tscm</SubscriptionArn>'
               '</ConfirmSubscriptionResult></ConfirmSubscriptionResponse>')

    def setUp(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.owner = User.objects.create_user(username="sns-owner@example.com", password="pw-sns")
        self.auditor = User.objects.create_user(username="sns-auditor@example.com", password="pw-sns-a")
        self.org = Organization.objects.create(legal_name="Signal LLC", display_name="Signal",
            slug="signal-sns")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.Role.OWNER)
        Membership.objects.create(user=self.auditor, organization=self.org, role=Membership.Role.AUDITOR)

    def pending(self, url=None, *, provider="sns", applied=False):
        url = self.GOOD if url is None else url
        return DeliveryEvent.objects.create(organization=self.org, provider=provider,
            channel=MessageConsent.Channel.EMAIL, destination="tscm-handbook",
            kind=DeliveryEvent.Kind.STATUS,
            detail="Subscription confirmation pending an owner's click", applied=applied,
            raw={"subscribe_url_host": url.split("/")[2] if "://" in url else "",
                 "subscribe_url": url[:300], "topic": "arn:aws:sns:us-east-1:1:tscm"})

    def confirm(self, event, *, fetch=None):
        from .services import confirm_sns_subscription
        return confirm_sns_subscription(self.org, event, self.owner,
            fetch=fetch or (lambda url: (200, self.OK_BODY)))

    def test_amazons_own_endpoint_is_the_shape_that_is_allowed(self):
        from .services import sns_confirmation_target
        self.assertEqual(self.GOOD, sns_confirmation_target(self.GOOD))

    def test_the_metadata_address_and_every_trick_that_wears_its_clothes_is_refused_unopened(self):
        from django.core.exceptions import ValidationError
        from .services import confirm_sns_subscription
        opened = []

        def spy(url):
            opened.append(url)
            return 200, self.OK_BODY

        for hostile in (
            "https://169.254.169.254/latest/meta-data/iam/",
            "http://169.254.169.254/latest/meta-data/",
            "https://metadata.google.internal/",
            "https://evil.example/?path=/sns.us-east-1.amazonaws.com",
            "https://sns.us-east-1.amazonaws.com@evil.example/",
            "https://sns.us-east-1.amazonaws.com.evil.example/",
            "https://sns.us-east-1.internal/",
            "https://sns.us-east-1.amazonaws.com:8443/?Action=ConfirmSubscription&Token=x",
            "https://sns.us-east-1.amazonaws.com/confirm?Action=ConfirmSubscription&Token=x",
            "https://sns.us-east-1.amazonaws.com/?Action=DeleteTopic&Token=x",
            ""):
            with self.subTest(url=hostile):
                with self.assertRaises(ValidationError):
                    confirm_sns_subscription(self.org, self.pending(hostile), self.owner, fetch=spy)
        self.assertEqual([], opened, "a refused address still reached the socket")

    def test_a_confirmation_click_is_attributed_and_keeps_its_token_out_of_the_record(self):
        event = self.pending()
        self.confirm(event)
        event.refresh_from_db()
        self.assertTrue(event.applied)
        self.assertIn("arn:aws:sns", event.raw["subscription_arn"])
        audit = AuditEvent.objects.get(target_type="delivery_event", target_id=str(event.pk),
            action="message.subscription_confirmed")
        self.assertEqual(self.owner.pk, audit.actor.pk)
        self.assertNotIn("sekret", json.dumps(audit.metadata),
                         "the token alone is enough to confirm this subscription, so it is not history")
        self.assertEqual("sns.us-east-1.amazonaws.com", audit.metadata["host"])

    def test_a_redirect_is_not_read_as_confirmation(self):
        """The fetch does not follow redirects, and the proof that matters is that a 3xx is a *no*."""
        from django.core.exceptions import ValidationError
        event = self.pending()
        with self.assertRaises(ValidationError):
            self.confirm(event, fetch=lambda url: (302, "Location: http://169.254.169.254/"))
        event.refresh_from_db()
        self.assertFalse(event.applied)
        self.assertTrue(AuditEvent.objects.filter(action="message.subscription_confirm_failed",
                                                  target_id=str(event.pk)).exists())

    def test_only_a_pending_sns_event_can_be_confirmed(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.confirm(self.pending(provider="twilio"))
        with self.assertRaises(ValidationError):
            self.confirm(self.pending(applied=True))
        with self.assertRaises(ValidationError):
            self.confirm(self.pending(""))
        event = self.pending()
        self.confirm(event)
        with self.assertRaises(ValidationError):
            self.confirm(event)

    def test_a_fetch_that_cannot_reach_amazon_reports_the_failure_not_the_subscription(self):
        from django.core.exceptions import ValidationError

        def broken(url):
            raise OSError("no route")

        event = self.pending()
        with self.assertRaises(ValidationError):
            self.confirm(event, fetch=broken)
        event.refresh_from_db()
        self.assertFalse(event.applied)
        self.assertIn("Could not reach", event.detail)

    def test_the_page_lists_the_pending_topic_without_printing_the_token(self):
        event = self.pending()
        self.client.force_login(self.owner)
        answer = self.client.get(reverse("messaging_settings"))
        self.assertEqual(200, answer.status_code)
        self.assertContains(answer, "tscm-handbook")
        self.assertContains(answer, "sns.us-east-1.amazonaws.com")
        # The address carries the confirmation token, so the page names the host and never the link.
        self.assertNotIn("sekret", answer.content.decode())
        self.assertContains(answer, reverse("messaging_confirm_subscription", args=[event.pk]))

    def test_a_failed_click_through_the_page_still_leaves_its_record(self):
        """The transaction that should not be there.

        A view wrapped in `atomic` rolls the attempt's own row back while still carrying the refusal
        message, so an operator chasing "why did the bounce never arrive" is left with no evidence that
        anybody ever tried. This goes through the route rather than the service because it is the
        *route's* transaction that caused it. Only the socket is stood in for — nothing about the
        decision under test is faked, and the suite must not open a connection to Amazon.
        """
        from unittest import mock
        event = self.pending()
        self.client.force_login(self.owner)
        with mock.patch("core.services._sns_fetch", return_value=(302, "")) as fetched:
            answer = self.client.post(reverse("messaging_confirm_subscription", args=[event.pk]))
        self.assertEqual(1, fetched.call_count)
        self.assertEqual(302, answer.status_code)
        event.refresh_from_db()
        self.assertFalse(event.applied)
        self.assertIn("302", event.detail)
        self.assertTrue(AuditEvent.objects.filter(action="message.subscription_confirm_failed",
                                                  target_id=str(event.pk)).exists(),
                        "a click that failed left no trace, which is the failure this path had once")

    def test_the_confirmation_is_a_post_and_a_reader_cannot_click_it(self):
        event = self.pending()
        self.client.force_login(self.auditor)
        self.assertEqual(403, self.client.post(reverse("messaging_confirm_subscription",
            args=[event.pk])).status_code)
        event.refresh_from_db()
        self.assertFalse(event.applied)
        self.client.force_login(self.owner)
        self.assertEqual(405, self.client.get(reverse("messaging_confirm_subscription",
            args=[event.pk])).status_code)

    def test_another_tenant_s_pending_event_is_not_found_rather_than_confirmed(self):
        elsewhere = Organization.objects.create(legal_name="Elsewhere LLC", display_name="Elsewhere",
            slug="elsewhere-sns")
        foreign = DeliveryEvent.objects.create(organization=elsewhere, provider="sns",
            channel=MessageConsent.Channel.EMAIL, destination="their-topic",
            kind=DeliveryEvent.Kind.STATUS, raw={"subscribe_url": self.GOOD})
        self.client.force_login(self.owner)
        self.assertEqual(404, self.client.post(reverse("messaging_confirm_subscription",
            args=[foreign.pk])).status_code)
        foreign.refresh_from_db()
        self.assertFalse(foreign.applied)


class DispositionTenantGuardTest(TestCase):
    """The last tenant-linked table without a database guard, policed where the decision is kept.

    `views.disposition_request` resolves the document through the actor's own organization, so the
    application cannot create a disposition that points at another tenant's file — which is why this
    surfaced as a disclosed gap rather than an incident. The guard exists anyway because a disposition
    row is the paper trail for *destroying* a personnel record, and this schema's posture is that a
    reference that consequential does not rest on application code alone.

    MySQL-only, like every trigger assertion in this suite: sqlite never runs this SQL, so the CI
    `mysql` leg is what executes these. The same-tenant case is the control that keeps a guard which
    refuses everything from reading like a guard that works.
    """

    def setUp(self):
        import tempfile
        from django.contrib.auth import get_user_model
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from .services import store_person_document
        User = get_user_model()
        self.owner = User.objects.create_user(username="guard-owner@example.com", password="pw-guard")
        self.org = Organization.objects.create(legal_name="Guarded LLC", display_name="Guarded",
            slug="guarded-dispo")
        self.other = Organization.objects.create(legal_name="Unguarded LLC", display_name="Unguarded",
            slug="unguarded-dispo")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.Role.OWNER)
        Membership.objects.create(user=self.owner, organization=self.other, role=Membership.Role.OWNER)
        self.kind = DocumentType.objects.create(organization=self.org, name="Record", code="guard-rec")
        self.their_kind = DocumentType.objects.create(organization=self.other, name="Their record",
            code="guard-theirs")
        media = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(media.cleanup)
        # Rows made through the service so the audit chain under them is real, and a refusal here is
        # about this table's guard rather than about a malformed row.
        self.mine = store_person_document(organization=self.org, person=None, document_type=self.kind,
            upload=SimpleUploadedFile("mine.pdf", b"%PDF-1.4\nmine", content_type="application/pdf"),
            actor=self.owner)
        self.theirs = store_person_document(organization=self.other, person=None,
            document_type=self.their_kind,
            upload=SimpleUploadedFile("theirs.pdf", b"%PDF-1.4\ntheirs",
                                      content_type="application/pdf"), actor=self.owner)

    def require_mysql(self):
        from django.db import connection
        if connection.vendor != "mysql":
            self.skipTest("the tenant guards are MySQL triggers")

    def insert(self, organization, document):
        """Raw SQL, with `uuid.hex` for the UUID columns and `str()` for the user.

        The `char(32)` trap this suite has met twice: a model instance interpolated into the WHERE of
        the guard's own subselect matches nothing, so the BEFORE trigger never fires and the probe
        reports an absent guard rather than a broken probe. `requested_by_id` is the opposite case —
        the user's pk is a plain integer, so `.hex` on it is an `AttributeError`, not a database answer.
        """
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core_dispositionrequest (id,organization_id,document_id,action,reason,"
                "requested_by_id,approved_by_id,status,created_at,executed_at,restored_by_id,restored_at,"
                "restore_reason) VALUES (%s,%s,%s,'archive','Retention window closed',%s,NULL,'pending',"
                "NOW(6),NULL,NULL,NULL,'')",
                [uuid.uuid4().hex, organization.pk.hex, document.pk.hex, str(self.owner.pk)])

    def test_both_guards_are_installed_on_the_disposition_table(self):
        from django.db import connection
        self.require_mysql()
        with connection.cursor() as cursor:
            cursor.execute("SELECT TRIGGER_NAME FROM information_schema.triggers "
                           "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE='core_dispositionrequest'")
            names = sorted(row[0] for row in cursor.fetchall())
        self.assertEqual(["core_disposition_tenant_insert", "core_disposition_tenant_update"], names)

    def test_the_database_refuses_a_destruction_order_borrowing_another_tenant_s_file(self):
        from django.db import connection, transaction
        self.require_mysql()
        with self.assertRaises(Exception) as refused:
            with transaction.atomic():
                self.insert(self.org, self.theirs)
        self.assertIn("cross-tenant disposition reference", str(refused.exception),
                      "the refusal did not come from this table's own guard")
        self.assertEqual(0, DispositionRequest.objects.filter(organization=self.org).count(),
                         "the guard is a BEFORE trigger, so nothing should have landed")

    def test_the_same_tenant_still_files_a_disposition(self):
        from django.db import connection
        self.require_mysql()
        self.insert(self.org, self.mine)
        self.assertEqual(1, DispositionRequest.objects.filter(organization=self.org,
            document=self.mine).count())

    def test_an_update_that_walks_a_request_across_the_tenant_line_is_refused(self):
        from django.db import connection, transaction
        self.require_mysql()
        self.insert(self.org, self.mine)
        request_row = DispositionRequest.objects.get(organization=self.org)
        with self.assertRaises(Exception) as refused:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE core_dispositionrequest SET document_id=%s WHERE id=%s",
                        [self.theirs.pk.hex, request_row.pk.hex])
        self.assertIn("cross-tenant disposition reference", str(refused.exception))
        request_row.refresh_from_db()
        self.assertEqual(self.mine.pk, request_row.document_id)


class TexasObligationsDraftTest(TestCase):
    """CMP-1: the control matrix gets its first content, and that content enforces nothing by itself.

    The product has always had the shape of a legal obligation and never an obligation, because encoding
    a jurisdiction's numbers without the licensee approving them is the thing this project refuses to do.
    The center of gravity here is therefore not "the rows arrive" — it is that a drafted row changes
    **no number anybody acts on** until a named person approves it, and that a row whose evidence the
    register cannot hold says so forever instead of quietly passing.

    One number in the seed corrects this file's own research pass: §1702.124(c) sets $100,000 / $50,000 /
    $200,000, not the $1M/$2M that brokers and client contracts treat as the Texas default. That is why
    the primary text is quoted on the row and the data test below demands the quotation be there.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from .scope import ActorScope
        User = get_user_model()
        self.ActorScope = ActorScope
        self.owner = User.objects.create_user(username="tx-owner@example.com", password="pw-tx")
        self.hr = User.objects.create_user(username="tx-hr@example.com", password="pw-tx-hr")
        self.auditor = User.objects.create_user(username="tx-auditor@example.com", password="pw-tx-au")
        self.officer_user = User.objects.create_user(username="tx-officer@example.com", password="pw-tx-o")
        self.org = Organization.objects.create(legal_name="Ranger LLC", display_name="Ranger",
            slug="ranger-tx")
        for user, role in ((self.owner, Membership.Role.OWNER), (self.hr, Membership.Role.HR),
                           (self.auditor, Membership.Role.AUDITOR),
                           (self.officer_user, Membership.Role.OFFICER)):
            Membership.objects.create(user=user, organization=self.org, role=role)
        self.ana = Person.objects.create(organization=self.org, user=self.officer_user, first_name="Ana",
            last_name="Perez", status=Person.Status.ACTIVE, is_unarmed_officer=True)
        from .texas_rules import seed_texas_obligations
        self.seed = seed_texas_obligations

    def attendance(self):
        from .services import compliance_attendance
        return compliance_attendance(self.org, self.ActorScope(None),
            reader=(Membership.Role.OWNER, None))

    def duties(self):
        from .services import compliance_duties
        return compliance_duties(self.org, self.ActorScope(None), list(Person.objects.filter(
            organization=self.org)), reader=(Membership.Role.OWNER, None))

    def test_drafting_the_duties_leaves_every_one_of_them_unenforced(self):
        self.seed(self.org, self.owner)
        rules = ComplianceRule.objects.filter(organization=self.org)
        self.assertEqual(5, rules.count())
        for rule in rules:
            self.assertFalse(rule.is_approved,
                             f"{rule.code} arrived enforceable, which is exactly what the ruling forbids")
            self.assertIsNone(rule.approved_by_id)
            self.assertIsNone(rule.approved_at)
            self.assertEqual("not approved, so not enforced yet", rule.unevaluated_reason)
        summary = self.duties()
        self.assertEqual(0, summary["total"],
                         "an unapproved draft entered the denominator, so the compliance rate would move "
                         "on the day somebody merely typed the obligation down")
        self.assertEqual(5, summary["unmeasured"])
        self.assertEqual(0, summary["attention"])

    def test_the_numbers_somebody_acts_on_do_not_move_when_the_drafts_land(self):
        """The invariant the whole design rests on, measured rather than asserted in prose."""
        from .services import record_punch
        from uuid import uuid4
        from django.utils import timezone
        before = self.attendance()
        site = Site.objects.create(organization=self.org, client=Client.objects.create(
            organization=self.org, name="Gate"), name="Front", address="1 Test St")
        post = Shift.objects.create(organization=self.org, site=site, officer=self.ana,
            starts_at=timezone.now(), ends_at=timezone.now() + timezone.timedelta(hours=8),
            status=Shift.Status.PUBLISHED)
        self.seed(self.org, self.owner)
        after = self.attendance()
        # `compliance_attendance` is the one definition the queue, the dashboard tile and the report all
        # read, so comparing its per-kind figures before and after is the whole claim: typing an
        # obligation onto the matrix must not move a number anybody acts on.
        self.assertEqual({key: (value["total"], value["attention"]) for key, value in before.items()},
            {key: (value["total"], value["attention"]) for key, value in after.items()},
            "a drafted-but-unapproved duty moved a figure the compliance queue is worked from")
        self.assertEqual(CredentialType.objects.filter(organization=self.org).count(), 0,
            "a drafted registration duty seeded a credential requirement, and the clock gate reads "
            "blocks_clock_in without asking whether anyone approved it")
        punch, created = record_punch(organization=self.org, person=self.ana, shift=post, source="pwa",
            client_event_id=uuid4(), kind=Punch.Kind.IN, occurred_at=timezone.now(), actor=self.owner)
        self.assertTrue(created, "a drafted duty stopped a clock-in that no approval had authorized")

    def test_re_running_the_draft_adds_nothing_and_rewrites_nothing(self):
        self.seed(self.org, self.owner)
        liability = ComplianceRule.objects.get(organization=self.org, code="tx_gl_insurance")
        liability.warning_days = 45
        liability.name = "General liability certificate (our broker's limit)"
        liability.save()
        again = self.seed(self.org, self.owner)
        self.assertEqual([], again["rules"], "a re-seed claimed it had drafted rows that already existed")
        self.assertEqual(5, ComplianceRule.objects.filter(organization=self.org).count())
        liability.refresh_from_db()
        self.assertEqual(45, liability.warning_days,
                         "the re-seed overwrote an edit the company had already made to its own row")
        self.assertEqual("General liability certificate (our broker's limit)", liability.name)

    def test_a_duty_the_company_already_entered_by_hand_is_not_duplicated(self):
        from .texas_rules import missing_texas_duties
        ComplianceRule.objects.create(organization=self.org, name="Our liability check",
            code="tx_gl_insurance", evidence=ComplianceRule.Evidence.DOCUMENT,
            applies_to_subject=ComplianceRule.Subject.ORGANIZATION)
        self.assertEqual(4, len(missing_texas_duties(self.org)))
        result = self.seed(self.org, self.owner)
        self.assertEqual(4, len(result["rules"]))
        self.assertEqual(1, ComplianceRule.objects.filter(organization=self.org,
            code="tx_gl_insurance").count())

    def test_every_drafted_row_carries_the_text_it_claims_to_stand_on(self):
        """A row is approvable only with a source, a reference and a reading — this checks the seed
        itself, because `is_approved` would let a sloppy row be approved into enforcement on a link and
        a one-word interpretation, and the person clicking Approve is trusting the drafting."""
        from .models import PERSONNEL_CATEGORIES
        from .texas_rules import DOCUMENT_TYPES, DUTIES
        type_codes = {spec["code"] for spec in DOCUMENT_TYPES}
        for spec in DUTIES:
            with self.subTest(code=spec["code"]):
                self.assertTrue(spec["authority_url"].startswith("https://"))
                reference = spec["authority_reference"]
                departmental = reference.startswith("Tex. DPS")
                self.assertTrue("§" in reference or departmental,
                    "a reference with no section number and no departmental label cannot be checked")
                if departmental:
                    # The one drafted duty sourced from the department's published procedure rather than
                    # the statute or the rule must say so in three places, because a reader who takes it
                    # for §1702 text is quoting something that does not exist.
                    self.assertIn("dps.texas.gov", spec["authority_url"])
                    self.assertIn("not the statute", spec["interpretation"])
                    self.assertIn("departmental procedure", spec["interpretation"])
                self.assertGreater(len(spec["interpretation"]), 400,
                                   "a one-line paraphrase is not a reading of the source")
                self.assertIn('"', spec["interpretation"],
                               "the interpretation restates rather than quotes, so nothing anchors it")
                if spec["subject"] == ComplianceRule.Subject.PEOPLE:
                    self.assertTrue(set(spec["applies_to"]) <= {code for code, _ in PERSONNEL_CATEGORIES},
                                    "a category code that is not in the model binds nobody")
                    self.assertTrue(spec["applies_to"])
                if spec["evidence"] == ComplianceRule.Evidence.DOCUMENT:
                    self.assertIn(spec["document_type"], type_codes,
                                  "a filed-record duty with no seeded record type can never be measured")
        rule = DocumentType._meta.get_field("audience")
        self.assertTrue(rule.choices, "the seeded types must declare who they are issued to")

    def test_filing_the_evidence_after_approving_makes_the_duty_answer_instead_of_vanishing(self):
        """The proof that the seed is not decorative: approval plus a record produces a real verdict."""
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from django.utils import timezone
        self.seed(self.org, self.owner)
        rule = ComplianceRule.objects.get(organization=self.org, code="tx_gl_insurance")
        rule.approved_by = self.owner
        rule.approved_at = timezone.now()
        rule.save()
        missing = self.duties()
        states = {row["state"] for row in missing["rows"] if row["subject"] == rule.name}
        self.assertIn("missing", states, "an approved company duty with nothing filed reported no gap")
        self.assertGreaterEqual(missing["total"], 1)
        media = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(media.cleanup)
        from .services import store_person_document
        store_person_document(organization=self.org, person=None, document_type=rule.document_type,
            upload=SimpleUploadedFile("gl.pdf", b"%PDF-1.4\ncertificate",
                                      content_type="application/pdf"), actor=self.owner)
        after = self.duties()
        states = {row["state"] for row in after["rows"] if row["subject"] == rule.name}
        self.assertEqual({"active"}, states, "the filed certificate did not satisfy the duty it proves")

    def test_a_duty_the_register_cannot_hold_still_says_so_after_approval(self):
        self.seed(self.org, self.owner)
        posting = ComplianceRule.objects.get(organization=self.org, code="tx_license_posted")
        posting.approved_by = self.owner
        from django.utils import timezone
        posting.approved_at = timezone.now()
        posting.save()
        summary = self.duties()
        rows = [row for row in summary["rows"] if row["subject"] == posting.name]
        self.assertEqual("not-evaluated", rows[0]["state"])
        self.assertIn("records cannot be filed against a site yet", rows[0]["note"])
        self.assertEqual(0, summary["total"],
                         "a duty nobody can measure joined the denominator and diluted the rate")

    def test_each_draft_is_stamped_as_version_one_before_anybody_approves_it(self):
        self.seed(self.org, self.owner)
        for rule in ComplianceRule.objects.filter(organization=self.org):
            version_one = RuleRevision.objects.filter(organization=self.org,
                kind=RuleRevision.Kind.COMPLIANCE_RULE, rule_id=str(rule.pk), revision=1).first()
            self.assertIsNotNone(version_one, f"{rule.code} has no version 1, so its first approval "
                                              "would be a change with nothing to differ from")
            self.assertEqual(rule.interpretation, version_one.values["interpretation"])

    def test_the_drafting_is_attributed_and_records_that_nothing_was_approved(self):
        self.seed(self.org, self.owner)
        event = AuditEvent.objects.get(action="compliance_rules.drafted",
            target_id=str(self.org.pk))
        self.assertEqual(self.owner.pk, event.actor.pk)
        self.assertFalse(event.metadata["approved"])
        self.assertEqual(5, len(event.metadata["rules"]))
        self.assertIn("gl_certificate", event.metadata["record_types"])

    def test_the_command_is_the_same_write_as_the_button(self):
        """The headless path is the one an operator uses before a first tenant exists, so it is tested
        rather than assumed: the button and the command must produce the same rows, and the dry run must
        prove nothing was written instead of claiming it."""
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("seed_texas_obligations", "--organization", self.org.slug, "--dry-run", stdout=out)
        self.assertIn("5 duty", out.getvalue())
        self.assertEqual(0, ComplianceRule.objects.filter(organization=self.org).count(),
            "--dry-run wrote rows")
        out = StringIO()
        call_command("seed_texas_obligations", "--organization", self.org.slug, stdout=out)
        self.assertEqual(5, ComplianceRule.objects.filter(organization=self.org).count())
        self.assertIn("none of it is enforced", out.getvalue().lower())
        # A second run says so instead of silently doing nothing at all.
        out = StringIO()
        call_command("seed_texas_obligations", "--organization", self.org.slug, stdout=out)
        self.assertIn("already present", out.getvalue())
        self.assertEqual(5, ComplianceRule.objects.filter(organization=self.org).count())
        with self.assertRaises(Exception):
            call_command("seed_texas_obligations", "--organization", "no-such-company")

    def test_the_screen_offers_the_drafts_and_a_reader_cannot_take_them_on(self):
        self.client.force_login(self.owner)
        answer = self.client.get(reverse("settings_compliance"))
        self.assertEqual(200, answer.status_code)
        self.assertContains(answer, "Draft these 5 obligations")
        self.assertContains(answer, "1702.124(c)")
        for user in (self.auditor, self.hr):
            self.client.force_login(user)
            self.assertEqual(403, self.client.post(reverse("compliance_seed_texas")).status_code)
            self.assertEqual(0, ComplianceRule.objects.filter(organization=self.org).count(),
                             "the refusal stopped the page but not the write")
        self.client.force_login(self.owner)
        self.assertEqual(405, self.client.get(reverse("compliance_seed_texas")).status_code)
        answer = self.client.post(reverse("compliance_seed_texas"), follow=True)
        self.assertEqual(5, ComplianceRule.objects.filter(organization=self.org).count())
        self.assertContains(answer, "None of them is enforced yet")
        self.assertNotContains(answer, "Draft these 5 obligations",
            msg_prefix="the offer stayed on the page after every duty had been drafted")


class ApprovalGatesEnforcementTest(TestCase):
    """The owner's ruling of 2026-10-05: a requirement nobody approved may not refuse anything.

    Enforcement is where this product says *no* to a human about their job, and until now the clock and
    schedule gates read `blocks_clock_in` / `blocks_scheduling` and `active` without asking whether the
    row behind them had a source, a reference, or a named approver. "Approved" meant one thing for a
    `ComplianceRule` and another for a `CredentialType` on the same matrix.

    The other half of the ruling is what makes this class exist: flipping the gate must **not** stop a
    requirement that has been refusing clock-ins for months. So the legacy escape defaults to True, the
    settings screen clears it for rows it creates, approval clears it for rows it adopts, and a plain
    edit leaves a grandfathered row alone. Each state is asserted in both directions, because the failure
    is silent either way — a punch that used to be refused is now accepted, or the reverse.
    """

    def setUp(self):
        from datetime import timedelta
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        User = get_user_model()
        self.timedelta, self.timezone = timedelta, timezone
        self.owner = User.objects.create_user(username="gate-owner@example.com", password="pw-gate")
        self.officer_user = User.objects.create_user(username="gate-officer@example.com",
            password="pw-gate-o")
        self.org = Organization.objects.create(legal_name="Turnstile LLC", display_name="Turnstile",
            slug="turnstile-gate")
        Membership.objects.create(user=self.owner, organization=self.org, role=Membership.Role.OWNER)
        Membership.objects.create(user=self.officer_user, organization=self.org,
            role=Membership.Role.OFFICER)
        self.ana = Person.objects.create(organization=self.org, user=self.officer_user,
            first_name="Ana", last_name="Torres", status=Person.Status.ACTIVE, is_unarmed_officer=True)
        self.client_user = Client.objects.create(organization=self.org, name="Reception")
        # No coordinates on the site: the geofence rule never fires, so every refusal below came from the
        # credential rule and nothing else.
        self.site = Site.objects.create(organization=self.org, client=self.client_user,
            name="Tower", address="1 Test St")
        self.now = timezone.localtime().replace(microsecond=0, second=0)
        self.post = Shift.objects.create(organization=self.org, site=self.site, officer=self.ana,
            starts_at=self.now - self.timedelta(hours=1), ends_at=self.now + self.timedelta(hours=7),
            status=Shift.Status.PUBLISHED)

    def requirement(self, *, grandfathered=True, approved=False, blocks_clock_in=True,
                    blocks_scheduling=True, applies=("unarmed",), name="Guard registration"):
        credential_type = CredentialType.objects.create(organization=self.org, name=name,
            code=name.lower().replace(" ", "-"), applies_to=list(applies),
            blocks_clock_in=blocks_clock_in, blocks_scheduling=blocks_scheduling,
            enforcement_grandfathered=grandfathered,
            authority_url="https://www.dps.texas.gov/section/private-security",
            authority_reference="Tex. Occ. Code § 1702.222",
            interpretation="Officers in the named categories must hold current registration.")
        if approved:
            # `is_approved` needs the whole set — approver, date, source URL, section reference and the
            # firm's reading — so an approval that only stamps a name and a time is not one, and this
            # fixture would be asserting the wrong thing.
            credential_type.approved_by = self.owner
            credential_type.approved_at = self.now
            credential_type.enforcement_grandfathered = False
            credential_type.save()
        return credential_type

    def expired_credential(self, credential_type):
        # `expired` is a derived state, not a stored one: an active row whose expiry date has passed
        # reads as expired through `effective_status`, which is what both gates consult.
        return Credential.objects.create(organization=self.org, person=self.ana,
            credential_type=credential_type, status=Credential.Status.ACTIVE,
            issued_on=self.now.date() - self.timedelta(days=300),
            expires_on=self.now.date() - self.timedelta(days=4))

    def punch(self):
        from .services import record_punch
        return record_punch(organization=self.org, person=self.ana, shift=self.post, source="pwa",
            client_event_id=uuid.uuid4(), kind=Punch.Kind.IN, occurred_at=self.now,
            actor=self.officer_user)

    def eligible(self, credential_type, purpose):
        from .services import shift_eligibility
        return shift_eligibility(self.post, officer=self.ana, purpose=purpose,
            requirements=[(credential_type, "site")])

    def form_payload(self, approve=False):
        payload = {"name": "Guard registration", "code": "guard-reg", "jurisdiction": "Texas",
            "authority_url": "https://www.dps.texas.gov/section/private-security",
            "authority_reference": "Tex. Occ. Code § 1702.222",
            "interpretation": "Unarmed officers must hold current registration.",
            "effective_from": "", "effective_until": "", "blocks_scheduling": "on",
            "blocks_clock_in": "on", "warning_days": 30, "reminder_days_before": "90, 60",
            "evidence_required": "on", "applies_to": ["unarmed"], "active": "on"}
        if approve:
            payload["approve"] = "yes"
        return payload

    def test_a_draft_requirement_refuses_nothing_at_the_clock_or_the_assignment(self):
        requirement = self.requirement(grandfathered=False)
        self.expired_credential(requirement)
        self.assertFalse(requirement.is_approved)
        self.assertFalse(requirement.may_enforce,
                         "a row with neither an approval nor the legacy escape still gates, which is the "
                         "behaviour this ruling removes")
        punch, created = self.punch()
        self.assertTrue(created, "an unapproved draft blocked a punch")
        self.assertFalse(punch.exception_reason, "the draft wrote a review exception")
        clean, reasons = self.eligible(requirement, "schedule")
        self.assertTrue(clean, f"a draft blocked assignment: {reasons}")

    def test_the_same_requirement_refuses_once_somebody_approves_it(self):
        requirement = self.requirement(grandfathered=False)
        self.expired_credential(requirement)
        requirement.approved_by = self.owner
        requirement.approved_at = self.now
        requirement.save()
        self.assertTrue(requirement.may_enforce)
        with self.assertRaises(Exception) as refused:
            self.punch()
        self.assertIn("Guard registration", str(refused.exception),
                      "the refusal did not name the requirement, so a dispatcher could not act on it")
        clean, _reasons = self.eligible(requirement, "schedule")
        self.assertFalse(clean)

    def test_a_requirement_that_predates_the_gate_keeps_refusing(self):
        """The no-silent-weakening test: what an upgrade day must not change."""
        requirement = self.requirement(grandfathered=True)
        self.expired_credential(requirement)
        self.assertFalse(requirement.is_approved)
        self.assertTrue(requirement.may_enforce,
                        "a requirement that has been refusing clock-ins stopped enforcing when the "
                        "approval gate was introduced")
        with self.assertRaises(Exception):
            self.punch()
        clean, _reasons = self.eligible(requirement, "schedule")
        self.assertFalse(clean)

    def test_creating_a_requirement_from_the_screen_does_not_inherit_the_legacy_escape(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("credential_type_create"), self.form_payload())
        created = CredentialType.objects.get(organization=self.org, code="guard-reg")
        self.assertFalse(created.enforcement_grandfathered,
                         "the column's default leaked onto a row created after the ruling, so it gates on "
                         "an obligation nobody approved")
        self.assertFalse(created.is_approved)
        self.expired_credential(created)
        _punch, made = self.punch()
        self.assertTrue(made, "a row the screen created as a draft still refused the punch")

    def test_approving_clears_the_escape_and_a_plain_edit_does_not(self):
        self.client.force_login(self.owner)
        legacy = self.requirement(grandfathered=True, name="Firearms discharge licence")
        self.client.post(reverse("credential_type_edit", args=[legacy.pk]), self.form_payload())
        legacy.refresh_from_db()
        self.assertTrue(legacy.enforcement_grandfathered,
                        "editing a row's spelling stopped it enforcing")
        self.client.post(reverse("credential_type_edit", args=[legacy.pk]),
                         self.form_payload(approve=True))
        legacy.refresh_from_db()
        self.assertTrue(legacy.is_approved)
        self.assertFalse(legacy.enforcement_grandfathered,
            "an approved row still carrying the legacy flag has two reasons it enforces, and a later "
            "reader cannot tell which one the firm meant")
        self.assertTrue(legacy.may_enforce, "clearing the escape on approval stopped enforcement")

    def test_the_matrix_shows_which_rows_are_gating_and_why(self):
        self.requirement(grandfathered=True, name="Firearms discharge licence")
        self.requirement(grandfathered=False, name="Guard registration")
        self.requirement(grandfathered=False, approved=True, name="PSB licence")
        self.requirement(grandfathered=True, approved=False, blocks_clock_in=False,
            blocks_scheduling=False, name="Agency certificate")
        self.client.force_login(self.owner)
        page = self.client.get(reverse("settings_compliance")).content.decode()
        # Three labels, and each row's sub-label must agree with the label above it. The last fixture is
        # the grandfathered-but-no-flags case: the escape is irrelevant when nothing is asked to gate,
        # and a screen that flagged it as unpaid debt would be crying wolf.
        self.assertIn("Schedule Clock-in · grandfathered", page)
        self.assertIn("Schedule Clock-in · not gating", page)
        self.assertIn("Enforcing without approval — approve it or clear the flags", page)
        self.assertIn("Draft: recorded, not gating", page)
        self.assertIn("Enforced at assignment and clock-in", page)
        self.assertIn("Recorded only", page)
        self.assertEqual(1, page.count("Enforcing without approval"),
                         "the overstated label is back: only the grandfathered row is enforcing without "
                         "approval, and the draft must not read that way")

    def test_the_ruling_leaves_both_halves_of_the_matrix_inert_until_approval(self):
        """CMP-1's safety property, now true of requirements as well as duties — CMP-0 asked for one
        matrix with one meaning, and this is where the two halves had diverged.
        """
        from .texas_rules import seed_texas_obligations
        seed_texas_obligations(self.org, self.owner)
        requirement = self.requirement(grandfathered=False)
        requirement.approved_by = self.owner
        requirement.approved_at = self.now
        requirement.save()
        self.expired_credential(requirement)
        for rule in ComplianceRule.objects.filter(organization=self.org):
            self.assertFalse(rule.is_approved)
            self.assertEqual("not approved, so not enforced yet", rule.unevaluated_reason)
        with self.assertRaises(Exception):
            self.punch()
