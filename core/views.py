import json
import uuid
from datetime import datetime, timedelta
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core import signing
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from .auth import membership_required
from .forms import AuditRedactionForm, BrandForm, BranchForm, ClientForm, CredentialForm, CredentialTypeForm, CsvImportForm, CustomFieldDefinitionForm, DispositionRequestForm, DocumentAcknowledgmentForm, DocumentTypeForm, DocumentUploadForm, DomainForm, InvitationAcceptanceForm, MembershipInvitationForm, OrganizationSecurityForm, PayrollPeriodForm, PersonForm, PunchAdjustmentForm, ShiftForm, SiteForm, TimePolicyForm, TrainingRecordForm
from .models import AuditEvent, AuditRedaction, BrandVersion, Checkpoint, Client, Credential, CredentialType, CustomFieldDefinition, DispositionRequest, DocumentAcknowledgment, DocumentType, ImportBatch, Membership, MembershipInvitation, Notification, OfflineClockDevice, Organization, OrganizationDomain, PayrollRun, Person, PersonCustomValue, PersonDocument, Punch, PunchAdjustment, Shift, Site, TimePolicy, TrainingRecord
from .services import apply_csv_import, approve_payroll_run, coerce_custom_value, create_brand_version, create_payroll_run, execute_disposition, payroll_csv, payroll_snapshot_csv, payroll_snapshot_pdf, payroll_snapshot_xlsx, person_snapshot, preview_csv_import, process_brand_image, record_person_history, record_punch, shift_eligibility, store_person_document, verify_audit_chain

PRIVILEGED = (Membership.Role.OWNER, Membership.Role.ADMIN)
MANAGERS = PRIVILEGED + (Membership.Role.HR, Membership.Role.SCHEDULER, Membership.Role.SUPERVISOR)

def health(request):
    return JsonResponse({"status": "ok", "service": "texas-security-company-manager"})

def ready(request):
    from django.core.cache import cache
    from django.db import connection
    try:
        with connection.cursor() as cursor: cursor.execute("SELECT 1"); cursor.fetchone()
        cache.set("readiness-probe","ok",10)
        if cache.get("readiness-probe")!="ok": raise RuntimeError("cache unavailable")
    except Exception:
        return JsonResponse({"status":"unavailable"},status=503)
    return JsonResponse({"status":"ready"})

@membership_required()
def theme_css(request):
    org=request.organization
    css=f":root{{--brand:{org.primary_color};--accent:{org.accent_color}}}"
    if org.dark_mode_enabled:
        css+=f"@media(prefers-color-scheme:dark){{:root{{--brand:{org.dark_primary_color};--accent:{org.dark_accent_color};--canvas:#0b1220;--surface:#111c2d;--ink:#f5f7fa;--muted:#aab6c7;--line:#2b3a50}}}}"
    return HttpResponse(css,content_type="text/css",headers={"Cache-Control":"private, max-age=300","X-Content-Type-Options":"nosniff"})

@membership_required()
def dashboard(request):
    org = request.organization
    context = {
        "people_count": org.people.count(), "branch_count": org.branches.filter(active=True).count(),
        "onboarding_count": org.people.filter(status=Person.Status.ONBOARDING).count(),
        "recent_people": org.people.select_related("branch")[:5],
        "recent_events": org.audit_events.select_related("actor")[:6],
    }
    return render(request, "core/dashboard.html", context)

@membership_required(*PRIVILEGED)
@transaction.atomic
def team(request):
    form = MembershipInvitationForm(request.POST or None)
    invitation_url = request.session.pop("new_invitation_url", None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        if request.organization.memberships.filter(user__email__iexact=email, active=True).exists():
            form.add_error("email", "This person is already an active member.")
        else:
            replaced = list(MembershipInvitation.objects.filter(organization=request.organization, email__iexact=email, accepted_at__isnull=True))
            for previous in replaced:
                AuditEvent.objects.create(organization=request.organization, actor=request.user, action="membership.invitation_replaced", target_type="membership_invitation", target_id=str(previous.pk), metadata={"email": email})
                previous.delete()
            invitation, token = MembershipInvitation.issue(organization=request.organization, email=email, role=form.cleaned_data["role"], invited_by=request.user, expires_at=timezone.now() + timedelta(hours=72))
            AuditEvent.objects.create(organization=request.organization, actor=request.user, action="membership.invited", target_type="membership_invitation", target_id=str(invitation.pk), metadata={"email": email, "role": invitation.role})
            invitation_url=request.build_absolute_uri(reverse("invitation_accept", args=[token]))
            Notification.objects.create(organization=request.organization,destination=email,channel=Notification.Channel.EMAIL,event_type="membership.invitation",subject=f"Join {request.organization.display_name}",body=f"You were invited as {invitation.get_role_display()}. Accept this single-use invitation within 72 hours: {invitation_url}",deduplication_key=f"membership-invitation:{invitation.pk}")
            request.session["new_invitation_url"] = invitation_url
            messages.success(request, "Invitation queued for delivery. The single-use link is also shown below once.")
            return redirect("team")
    return render(request, "core/team.html", {"form": form, "invitation_url": invitation_url, "memberships": request.organization.memberships.select_related("user"), "invitations": request.organization.membership_invitations.filter(accepted_at__isnull=True)})

@transaction.atomic
def invitation_accept(request, token):
    invitation = MembershipInvitation.objects.select_for_update().select_related("organization").filter(token_hash=MembershipInvitation.digest_token(token)).first()
    if not invitation or invitation.accepted_at or invitation.expires_at <= timezone.now():
        return render(request, "core/invitation_accept.html", {"invalid": True}, status=410)
    User = get_user_model()
    existing = User.objects.filter(email__iexact=invitation.email).first() or User.objects.filter(username__iexact=invitation.email).first()
    if existing and (not request.user.is_authenticated or request.user.pk != existing.pk):
        messages.info(request, "Sign in to the invited account before accepting this invitation.")
        return redirect(f'{reverse("account_login")}?next={request.path}')
    form = None if existing else InvitationAcceptanceForm(request.POST or None)
    if request.method == "POST" and (existing or form.is_valid()):
        user = existing
        if user is None:
            user = User(username=invitation.email, email=invitation.email, first_name=form.cleaned_data["first_name"], last_name=form.cleaned_data["last_name"])
            user.set_password(form.cleaned_data["password"])
            user.save()
        membership, _ = Membership.objects.update_or_create(organization=invitation.organization, user=user, defaults={"role": invitation.role, "active": True})
        invitation.accepted_at = timezone.now(); invitation.save(update_fields=["accepted_at"])
        AuditEvent.objects.create(organization=invitation.organization, actor=user, action="membership.invitation_accepted", target_type="membership", target_id=str(membership.pk), metadata={"role": membership.role})
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        request.session["active_organization_id"] = str(invitation.organization_id)
        messages.success(request, f"Welcome to {invitation.organization.display_name}.")
        return redirect("dashboard")
    return render(request, "core/invitation_accept.html", {"invitation": invitation, "form": form})

@membership_required(*MANAGERS)
def people(request):
    records = request.organization.people.select_related("branch")
    return render(request, "core/people.html", {"people": records})

@membership_required(*MANAGERS)
@transaction.atomic
def person_create(request):
    form = PersonForm(request.POST or None)
    form.fields["branch"].queryset = request.organization.branches.filter(active=True)
    if request.method == "POST" and form.is_valid():
        person = form.save(commit=False); person.organization = request.organization; person.save()
        AuditEvent.objects.create(organization=request.organization, actor=request.user, action="person.created", target_type="person", target_id=str(person.pk), metadata={"name": person.full_name})
        messages.success(request, f"{person.full_name} was added.")
        return redirect("people")
    return render(request, "core/form.html", {"form": form, "title": "Add person", "eyebrow": "Personnel directory"})

@membership_required()
def person_detail(request,person_id):
    person=request.organization.people.select_related("branch","user").filter(pk=person_id).first()
    if not person:raise Http404
    manager=request.membership.role in MANAGERS
    if not manager and person.user_id!=request.user.id:raise Http404
    values={item.definition_id:item for item in person.custom_values.select_related("definition")}
    custom=[]
    for definition in request.organization.custom_field_definitions.filter(active=True):
        if definition.sensitive and not manager:continue
        custom.append((definition,values.get(definition.id)))
    return render(request,"core/person_detail.html",{"person":person,"custom":custom,"manager":manager})

@membership_required(*MANAGERS)
@transaction.atomic
def person_edit(request,person_id):
    person=request.organization.people.filter(pk=person_id).first()
    if not person:raise Http404
    before=person_snapshot(person);definitions=request.organization.custom_field_definitions.filter(active=True)
    form=PersonForm(request.POST or None,instance=person);form.fields["branch"].queryset=request.organization.branches.filter(active=True)
    custom_errors=[]
    if request.method=="POST" and form.is_valid():
        values={}
        for definition in definitions:
            try:values[definition]=coerce_custom_value(definition,request.POST.get(f"custom_{definition.key}"))
            except ValidationError as exc:custom_errors.append(str(exc))
        if not custom_errors:
            person=form.save();record_person_history(person,before,request.user)
            for definition,value in values.items():PersonCustomValue.objects.update_or_create(organization=request.organization,person=person,definition=definition,defaults={"value":value})
            messages.success(request,"Personnel profile updated.");return redirect("person_detail",person_id=person.pk)
    current={item.definition.key:item.value for item in person.custom_values.select_related("definition")}
    return render(request,"core/person_edit.html",{"form":form,"person":person,"definitions":definitions,"current":current,"custom_errors":custom_errors})

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.HR)
def custom_field_create(request):
    form=CustomFieldDefinitionForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False);item.organization=request.organization;item.save();AuditEvent.objects.create(organization=request.organization,actor=request.user,action="custom_field.created",target_type="custom_field_definition",target_id=str(item.pk),metadata={"key":item.key});messages.success(request,"Custom field created.");return redirect("people")
    return render(request,"core/form.html",{"form":form,"title":"Add personnel field","eyebrow":"HCRM configuration"})

@membership_required(*PRIVILEGED)
@transaction.atomic
def branch_create(request):
    form = BranchForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        branch = form.save(commit=False); branch.organization = request.organization; branch.save()
        AuditEvent.objects.create(organization=request.organization, actor=request.user, action="branch.created", target_type="branch", target_id=str(branch.pk), metadata={"name": branch.name})
        messages.success(request, f"{branch.name} branch was created.")
        return redirect("dashboard")
    return render(request, "core/form.html", {"form": form, "title": "Add branch", "eyebrow": "Organization"})

@membership_required(*PRIVILEGED)
@transaction.atomic
def branding(request):
    org = request.organization
    before = {"display_name": org.display_name, "primary_color": org.primary_color, "accent_color": org.accent_color}
    form = BrandForm(request.POST or None, request.FILES or None, instance=org)
    if request.method == "POST" and form.is_valid():
        item=form.save(commit=False)
        if request.FILES.get("logo"):
            try: item.logo=process_brand_image(request.FILES["logo"])
            except ValidationError as exc:
                form.add_error("logo",exc)
                return render(request,"core/branding.html",{"form":form,"versions":org.brand_versions.all()[:10]})
        item.save();create_brand_version(org,request.user)
        after = {"display_name": org.display_name, "primary_color": org.primary_color, "accent_color": org.accent_color}
        AuditEvent.objects.create(organization=org, actor=request.user, action="branding.updated", target_type="organization", target_id=str(org.pk), metadata={"before": before, "after": after})
        messages.success(request, "Brand settings published.")
        return redirect("branding")
    return render(request, "core/branding.html", {"form": form,"versions":org.brand_versions.all()[:10]})

@require_POST
@membership_required(*PRIVILEGED)
@transaction.atomic
def brand_rollback(request,version_id):
    version=request.organization.brand_versions.filter(pk=version_id).first()
    if not version: raise Http404
    org=request.organization
    for field in ("display_name","primary_color","accent_color","dark_primary_color","dark_accent_color","dark_mode_enabled","support_email","support_phone","timezone"):
        if field in version.snapshot: setattr(org,field,version.snapshot[field])
    if "logo" in version.snapshot: org.logo=version.snapshot["logo"]
    org.save();new_version=create_brand_version(org,request.user)
    AuditEvent.objects.create(organization=org,actor=request.user,action="branding.rolled_back",target_type="brand_version",target_id=str(new_version.pk),metadata={"source_version":version.version})
    messages.success(request,f"Brand restored from version {version.version}.")
    return redirect("branding")

def _scoped_form(form_class, organization, *args, **kwargs):
    form=form_class(*args, **kwargs)
    for field, model in (("branch", organization.branches), ("client", organization.clients), ("site", organization.sites), ("officer", organization.people), ("person", organization.people), ("credential_type", organization.credential_types), ("required_credentials", organization.credential_types)):
        if field in form.fields:
            form.fields[field].queryset=model.filter(active=True) if hasattr(model.model,"active") else model.all()
    return form

@membership_required(*MANAGERS)
def locations(request):
    return render(request,"core/locations.html",{"clients":request.organization.clients.prefetch_related("sites")})

@membership_required(*PRIVILEGED)
@transaction.atomic
def client_create(request):
    form=ClientForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization; item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="client.created",target_type="client",target_id=str(item.pk),metadata={"name":item.name})
        messages.success(request,"Client created."); return redirect("locations")
    return render(request,"core/form.html",{"form":form,"title":"Add client","eyebrow":"Locations"})

@membership_required(*PRIVILEGED)
@transaction.atomic
def site_create(request):
    form=_scoped_form(SiteForm,request.organization,request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization; item.full_clean(); item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="site.created",target_type="site",target_id=str(item.pk),metadata={"name":item.name})
        messages.success(request,"Site created."); return redirect("locations")
    return render(request,"core/form.html",{"form":form,"title":"Add site","eyebrow":"Locations"})

@membership_required(*MANAGERS)
def compliance(request):
    credentials=request.organization.credentials.select_related("person","credential_type")
    return render(request,"core/compliance.html",{"credentials":credentials,"types":request.organization.credential_types.all()})

@membership_required(*PRIVILEGED)
@transaction.atomic
def credential_type_create(request):
    form=CredentialTypeForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization
        if request.POST.get("approve") == "yes": item.approved_by=request.user;item.approved_at=timezone.now()
        item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="credential_type.created",target_type="credential_type",target_id=str(item.pk),metadata={"name":item.name,"approved":item.is_approved,"authority":item.authority_reference})
        messages.success(request,"Credential type created."); return redirect("compliance")
    return render(request,"core/form.html",{"form":form,"title":"Add credential type","eyebrow":"Compliance","approval_control":True})

@membership_required(*MANAGERS)
@transaction.atomic
def credential_create(request):
    form=_scoped_form(CredentialForm,request.organization,request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization; item.full_clean(); item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="credential.created",target_type="credential",target_id=str(item.pk),metadata={"status":item.status})
        messages.success(request,"Credential recorded."); return redirect("compliance")
    return render(request,"core/form.html",{"form":form,"title":"Record credential","eyebrow":"Compliance"})

@membership_required(*MANAGERS)
def schedule(request):
    shifts=request.organization.shifts.select_related("site__client","officer").prefetch_related("required_credentials")
    return render(request,"core/schedule.html",{"shifts":shifts})

@membership_required(*MANAGERS)
@transaction.atomic
def shift_create(request):
    form=_scoped_form(ShiftForm,request.organization,request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization; item.full_clean(); item.save(); form.save_m2m()
        allowed,reasons=shift_eligibility(item)
        if item.status==Shift.Status.PUBLISHED and not allowed:
            item.delete()
            form.instance=Shift(organization=request.organization)
            for reason in reasons: form.add_error("officer",reason)
        else:
            AuditEvent.objects.create(organization=request.organization,actor=request.user,action="shift.created",target_type="shift",target_id=str(item.pk),metadata={"status":item.status})
            messages.success(request,"Shift saved."); return redirect("schedule")
    return render(request,"core/form.html",{"form":form,"title":"Create shift","eyebrow":"Scheduling"})

@membership_required()
def clock(request):
    person=request.organization.people.filter(user=request.user).first()
    shifts=person.shifts.filter(starts_at__lte=timezone.now()+timedelta(hours=12),ends_at__gte=timezone.now()-timedelta(hours=12)).select_related("site__client") if person else []
    recent=person.punches.select_related("shift__site")[:8] if person else []
    return render(request,"core/clock.html",{"person":person,"shifts":shifts,"recent":recent})

@require_POST
@membership_required()
def clock_device_enroll(request):
    person=request.organization.people.filter(user=request.user).first()
    if not person:return JsonResponse({"error":"Your login is not linked to a personnel record."},status=403)
    device=OfflineClockDevice.objects.create(organization=request.organization,person=person,user=request.user,label=request.headers.get("User-Agent","")[:120])
    token=signing.dumps({"device":str(device.pk),"organization":str(request.organization.pk),"user":request.user.pk},salt="offline-clock")
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="clock_device.enrolled",target_type="offline_clock_device",target_id=str(device.pk))
    return JsonResponse({"device_id":str(device.pk),"token":token,"sequence":0})

@csrf_exempt
@require_POST
@transaction.atomic
def offline_punch_sync(request):
    try:
        data=json.loads(request.body); claims=signing.loads(data.pop("device_token"),salt="offline-clock",max_age=60*60*24*30)
        device=OfflineClockDevice.objects.select_for_update().select_related("organization","person","user").get(pk=claims["device"],organization_id=claims["organization"],user_id=claims["user"],active=True)
        sequence=int(data["device_sequence"])
        existing=Punch.objects.filter(client_event_id=data.get("client_event_id"),organization=device.organization,person=device.person,device_id=device.id,device_sequence=sequence).first()
        if existing:return JsonResponse({"id":str(existing.pk),"created":False,"review_status":existing.review_status,"exception":existing.exception_reason})
        if sequence<=device.last_sequence: raise ValidationError("This device sequence was already processed.")
        shift=None
        if data.get("shift_id"):shift=device.organization.shifts.select_related("site").get(pk=data["shift_id"])
        checkpoint=None
        if data.get("checkpoint_code"):
            checkpoint=Checkpoint.objects.select_related("site").get(organization=device.organization,scan_code=data["checkpoint_code"],active=True)
            if not shift or shift.site_id!=checkpoint.site_id:raise ValidationError("Checkpoint does not belong to the selected shift site.")
            if data.get("kind")!=Punch.Kind.CHECKPOINT:raise ValidationError("A checkpoint code requires a checkpoint punch.")
            if checkpoint.latitude is not None and (data.get("latitude") is None or data.get("longitude") is None):raise ValidationError("Checkpoint location is required.")
            from .services import haversine_meters
            if checkpoint.latitude is not None and haversine_meters(data["latitude"],data["longitude"],checkpoint.latitude,checkpoint.longitude)>checkpoint.radius_meters:raise ValidationError("Checkpoint scan was outside its geofence.")
        occurred_at=datetime.fromisoformat(data["occurred_at"].replace("Z","+00:00"))
        punch,created=record_punch(organization=device.organization,person=device.person,shift=shift,client_event_id=uuid.UUID(data["client_event_id"]),kind=data["kind"],occurred_at=occurred_at,latitude=data.get("latitude"),longitude=data.get("longitude"),offline=bool(data.get("offline")),source="offline-pwa",actor=device.user)
        if created:
            punch.device_id=device.id;punch.device_sequence=sequence;punch.checkpoint=checkpoint;punch.save(update_fields=["device_id","device_sequence","checkpoint"])
        device.last_sequence=sequence;device.last_seen_at=timezone.now();device.save(update_fields=["last_sequence","last_seen_at"])
        return JsonResponse({"id":str(punch.pk),"created":created,"review_status":punch.review_status,"exception":punch.exception_reason},status=201 if created else 200)
    except (KeyError,ValueError,ValidationError,signing.BadSignature,OfflineClockDevice.DoesNotExist,Shift.DoesNotExist,Checkpoint.DoesNotExist) as exc:
        return JsonResponse({"error":str(exc)},status=400)

@require_POST
@membership_required()
def punch_api(request):
    person=request.organization.people.filter(user=request.user).first()
    if not person: return JsonResponse({"error":"Your login is not linked to a personnel record."},status=403)
    try:
        data=json.loads(request.body); shift=None
        if data.get("shift_id"): shift=request.organization.shifts.select_related("site").get(pk=data["shift_id"])
        occurred_at=datetime.fromisoformat(data["occurred_at"].replace("Z","+00:00"))
        punch,created=record_punch(organization=request.organization,person=person,shift=shift,client_event_id=uuid.UUID(data["client_event_id"]),kind=data["kind"],occurred_at=occurred_at,latitude=data.get("latitude"),longitude=data.get("longitude"),offline=bool(data.get("offline")),source="pwa",actor=request.user)
        return JsonResponse({"id":str(punch.pk),"created":created,"review_status":punch.review_status,"exception":punch.exception_reason},status=201 if created else 200)
    except (KeyError,ValueError,ValidationError,Shift.DoesNotExist) as exc:
        message=exc.message if hasattr(exc,"message") else str(exc)
        return JsonResponse({"error":message},status=400)

@membership_required(*PRIVILEGED)
def time_policy(request):
    policy,_=TimePolicy.objects.get_or_create(organization=request.organization,defaults={"timezone":request.organization.timezone})
    form=TimePolicyForm(request.POST or None,instance=policy)
    if request.method=="POST" and form.is_valid():
        form.save(); AuditEvent.objects.create(organization=request.organization,actor=request.user,action="time_policy.updated",target_type="time_policy",target_id=str(policy.pk)); messages.success(request,"Time policy saved."); return redirect("time_policy")
    return render(request,"core/form.html",{"form":form,"title":"Time and payroll policy","eyebrow":"Timekeeping"})

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.PAYROLL)
def payroll_export(request):
    try: start=datetime.fromisoformat(request.GET["start"]); end=datetime.fromisoformat(request.GET["end"])
    except (KeyError,ValueError):
        end=timezone.now(); start=end-timedelta(days=7)
    if timezone.is_naive(start): start=timezone.make_aware(start)
    if timezone.is_naive(end): end=timezone.make_aware(end)
    content=payroll_csv(request.organization,start,end)
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="payroll.exported",target_type="organization",target_id=str(request.organization.pk),metadata={"start":start.isoformat(),"end":end.isoformat()})
    response=HttpResponse(content,content_type="text/csv"); response["Content-Disposition"]='attachment; filename="payroll.csv"'; return response

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.HR)
def documents(request):
    records=request.organization.person_documents.filter(deleted_at__isnull=True).select_related("person","document_type","uploaded_by")
    return render(request,"core/documents.html",{"documents":records,"types":request.organization.document_types.all()})

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.HR)
@transaction.atomic
def document_type_create(request):
    form=DocumentTypeForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization; item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="document_type.created",target_type="document_type",target_id=str(item.pk),metadata={"name":item.name})
        messages.success(request,"Document type created."); return redirect("documents")
    return render(request,"core/form.html",{"form":form,"title":"Add document type","eyebrow":"HCRM records"})

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.HR)
@transaction.atomic
def document_upload(request):
    form=DocumentUploadForm(request.POST or None,request.FILES or None)
    form.fields["person"].queryset=request.organization.people.all(); form.fields["document_type"].queryset=request.organization.document_types.filter(active=True)
    if request.method=="POST" and form.is_valid():
        try:
            store_person_document(organization=request.organization,person=form.cleaned_data["person"],document_type=form.cleaned_data["document_type"],upload=form.cleaned_data["file"],actor=request.user,expires_on=form.cleaned_data["expires_on"])
        except ValidationError as exc: form.add_error("file",exc)
        else: messages.success(request,"Document uploaded and scanned."); return redirect("documents")
    return render(request,"core/form.html",{"form":form,"title":"Upload personnel document","eyebrow":"HCRM records"})

@membership_required()
def document_download(request,document_id):
    document=request.organization.person_documents.select_related("person").filter(pk=document_id,deleted_at__isnull=True,scan_status=PersonDocument.ScanStatus.CLEAN).first()
    if not document: raise Http404
    privileged=request.membership.role in (Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.HR,Membership.Role.AUDITOR)
    if not privileged and document.person.user_id!=request.user.id: raise Http404
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="document.downloaded",target_type="person_document",target_id=str(document.pk))
    response=FileResponse(document.file.open("rb"),as_attachment=True,filename=document.original_name,content_type="application/octet-stream")
    response["X-Content-Type-Options"]="nosniff"; response["Cache-Control"]="private, no-store"; return response

@membership_required()
def my_documents(request):
    person=request.organization.people.filter(user=request.user).first()
    records=person.documents.filter(deleted_at__isnull=True,scan_status=PersonDocument.ScanStatus.CLEAN).select_related("document_type") if person else []
    return render(request,"core/my_documents.html",{"person":person,"documents":records})

@membership_required()
@transaction.atomic
def document_acknowledge(request,document_id):
    import hashlib,hmac
    from django.conf import settings
    person=request.organization.people.filter(user=request.user).first();document=request.organization.person_documents.select_related("document_type").filter(pk=document_id,person=person,deleted_at__isnull=True,scan_status=PersonDocument.ScanStatus.CLEAN).first()
    if not document:raise Http404
    form=DocumentAcknowledgmentForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        signature=form.cleaned_data["signature_name"].strip()
        if document.document_type.signature_required and not signature:form.add_error("signature_name","A typed signature is required.")
        else:
            ip=request.META.get("REMOTE_ADDR","");ip_hash=hmac.new(settings.SECRET_KEY.encode(),ip.encode(),hashlib.sha256).hexdigest()
            item,_=DocumentAcknowledgment.objects.update_or_create(document=document,person=person,defaults={"organization":request.organization,"user":request.user,"signature_name":signature,"statement":"I reviewed and acknowledge this document.","document_sha256":document.sha256,"ip_hash":ip_hash})
            document.acknowledged_at=item.acknowledged_at;document.save(update_fields=["acknowledged_at"]);AuditEvent.objects.create(organization=request.organization,actor=request.user,action="document.acknowledged",target_type="person_document",target_id=str(document.pk),metadata={"sha256":document.sha256,"signed":bool(signature)});messages.success(request,"Acknowledgment recorded.");return redirect("my_documents")
    return render(request,"core/document_acknowledge.html",{"form":form,"document":document})

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def retention_review(request):
    due=request.organization.person_documents.filter(deleted_at__isnull=True,retain_until__isnull=False,retain_until__lte=timezone.localdate()).select_related("person","document_type")
    pending=request.organization.disposition_requests.select_related("document__person","requested_by","approved_by")[:100]
    return render(request,"core/retention.html",{"due":due,"pending":pending})

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
@transaction.atomic
def disposition_request(request,document_id):
    document=request.organization.person_documents.filter(pk=document_id,deleted_at__isnull=True).first()
    if not document: raise Http404
    form=DispositionRequestForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item=DispositionRequest.objects.create(organization=request.organization,document=document,requested_by=request.user,action=form.cleaned_data["action"],reason=form.cleaned_data["reason"])
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="document.disposition_requested",target_type="disposition_request",target_id=str(item.pk),metadata={"document":str(document.pk),"action":item.action})
        messages.success(request,"Disposition request created.");return redirect("retention_review")
    return render(request,"core/form.html",{"form":form,"title":"Request record disposition","eyebrow":"Retention"})

@require_POST
@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def disposition_execute(request,request_id):
    item=request.organization.disposition_requests.select_related("document").filter(pk=request_id).first()
    if not item: raise Http404
    try: execute_disposition(item,request.user)
    except ValidationError as exc: messages.error(request,str(exc))
    else: messages.success(request,"Disposition completed and tombstone recorded.")
    return redirect("retention_review")

@require_POST
@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def legal_hold_toggle(request,document_id):
    document=request.organization.person_documents.filter(pk=document_id,deleted_at__isnull=True).first()
    if not document: raise Http404
    before=document.legal_hold;document.legal_hold=not before;document.save(update_fields=["legal_hold"])
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="document.legal_hold_changed",target_type="person_document",target_id=str(document.pk),metadata={"before":before,"after":document.legal_hold})
    messages.success(request,"Legal hold updated.");return redirect("retention_review")

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.HR)
def training(request):
    records=request.organization.training_records.select_related("person")
    return render(request,"core/training.html",{"records":records})

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.HR)
@transaction.atomic
def training_create(request):
    form=TrainingRecordForm(request.POST or None); form.fields["person"].queryset=request.organization.people.all()
    if request.method=="POST" and form.is_valid():
        item=form.save(commit=False); item.organization=request.organization; item.full_clean(); item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="training.created",target_type="training_record",target_id=str(item.pk),metadata={"course":item.course_name})
        messages.success(request,"Training record added."); return redirect("training")
    return render(request,"core/form.html",{"form":form,"title":"Add training record","eyebrow":"Compliance"})

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.HR)
def imports(request):
    form=CsvImportForm(request.POST or None,request.FILES or None); batch=None
    if request.method=="POST" and form.is_valid():
        try: batch,_=preview_csv_import(organization=request.organization,entity=form.cleaned_data["entity"],upload=form.cleaned_data["file"],actor=request.user)
        except ValidationError as exc: form.add_error("file",exc)
    return render(request,"core/imports.html",{"form":form,"batch":batch,"batches":request.organization.import_batches.all()[:20]})

@membership_required(*MANAGERS)
def import_template(request,entity):
    from .services import IMPORT_COLUMNS
    columns=IMPORT_COLUMNS.get(entity)
    if not columns: raise Http404
    response=HttpResponse(",".join(sorted(columns))+"\n",content_type="text/csv")
    response["Content-Disposition"]=f'attachment; filename="{entity}-import-template.csv"'
    return response

@membership_required(*MANAGERS)
def import_errors(request,batch_id):
    import csv
    from io import StringIO
    batch=request.organization.import_batches.filter(pk=batch_id).first()
    if not batch: raise Http404
    output=StringIO();writer=csv.writer(output);writer.writerow(["row","errors"])
    for item in batch.errors:writer.writerow([item.get("row"),"; ".join(item.get("errors",[]))])
    response=HttpResponse(output.getvalue(),content_type="text/csv");response["Content-Disposition"]=f'attachment; filename="{batch.entity}-import-errors.csv"';return response

@require_POST
@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.HR)
def import_apply(request,batch_id):
    batch=request.organization.import_batches.filter(pk=batch_id).first()
    if not batch: raise Http404
    try: count=apply_csv_import(batch,request.user)
    except (ValidationError,Exception) as exc:
        messages.error(request,f"Import could not be applied: {exc}")
    else: messages.success(request,f"Imported {count} rows.")
    return redirect("imports")

@membership_required()
def notifications(request):
    items=request.user.workforce_notifications.filter(organization=request.organization)
    return render(request,"core/notifications.html",{"notifications":items})

@require_POST
@membership_required()
def notification_read(request,notification_id):
    item=request.user.workforce_notifications.filter(organization=request.organization,pk=notification_id).first()
    if not item: raise Http404
    if item.channel==Notification.Channel.IN_APP: item.status=Notification.Status.READ; item.save(update_fields=["status"])
    return redirect("notifications")

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.PAYROLL,Membership.Role.SUPERVISOR)
def time_review(request):
    punches=request.organization.punches.select_related("person","shift__site").order_by("-occurred_at")[:250]
    adjustments=request.organization.punch_adjustments.select_related("punch__person","requested_by","reviewed_by")[:100]
    return render(request,"core/time_review.html",{"punches":punches,"adjustments":adjustments})

@require_POST
@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.PAYROLL,Membership.Role.SUPERVISOR)
@transaction.atomic
def punch_review(request,punch_id):
    punch=request.organization.punches.filter(pk=punch_id).first()
    if not punch: raise Http404
    action=request.POST.get("action"); reason=request.POST.get("reason","").strip()
    if action not in (Punch.Review.ACCEPTED,Punch.Review.REJECTED) or (action==Punch.Review.REJECTED and len(reason)<5):
        messages.error(request,"Choose approve/reject and provide a rejection reason."); return redirect("time_review")
    before=punch.review_status;punch.review_status=action;punch.exception_reason=reason;punch.save(update_fields=["review_status","exception_reason"])
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="punch.reviewed",target_type="punch",target_id=str(punch.pk),metadata={"before":before,"after":action,"reason":reason})
    messages.success(request,"Punch review saved."); return redirect("time_review")

@membership_required()
@transaction.atomic
def adjustment_request(request,punch_id):
    person=request.organization.people.filter(user=request.user).first(); punch=request.organization.punches.filter(pk=punch_id,person=person).first()
    if not punch: raise Http404
    locked=request.organization.payroll_runs.filter(status__in=[PayrollRun.Status.APPROVED,PayrollRun.Status.EXPORTED],period_start__lte=punch.occurred_at,period_end__gt=punch.occurred_at).exists()
    if locked: messages.error(request,"This payroll period is locked."); return redirect("clock")
    form=PunchAdjustmentForm(request.POST or None,initial={"proposed_at":punch.occurred_at})
    if request.method=="POST" and form.is_valid():
        item=PunchAdjustment.objects.create(organization=request.organization,punch=punch,requested_by=request.user,proposed_at=form.cleaned_data["proposed_at"],reason=form.cleaned_data["reason"])
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="punch_adjustment.requested",target_type="punch_adjustment",target_id=str(item.pk),metadata={"punch":str(punch.pk)})
        messages.success(request,"Correction requested."); return redirect("clock")
    return render(request,"core/form.html",{"form":form,"title":"Request time correction","eyebrow":"Timekeeping"})

@require_POST
@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.PAYROLL,Membership.Role.SUPERVISOR)
@transaction.atomic
def adjustment_review(request,adjustment_id):
    item=request.organization.punch_adjustments.select_related("punch").filter(pk=adjustment_id,status=PunchAdjustment.Status.REQUESTED).first()
    if not item: raise Http404
    status=request.POST.get("action"); note=request.POST.get("note","").strip()
    if status not in (PunchAdjustment.Status.APPROVED,PunchAdjustment.Status.REJECTED): messages.error(request,"Invalid review action."); return redirect("time_review")
    locked=request.organization.payroll_runs.filter(status__in=[PayrollRun.Status.APPROVED,PayrollRun.Status.EXPORTED],period_start__lte=item.punch.occurred_at,period_end__gt=item.punch.occurred_at).exists()
    if locked: messages.error(request,"This payroll period is locked."); return redirect("time_review")
    item.status=status;item.reviewed_by=request.user;item.reviewed_at=timezone.now();item.review_note=note;item.save(update_fields=["status","reviewed_by","reviewed_at","review_note"])
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="punch_adjustment.reviewed",target_type="punch_adjustment",target_id=str(item.pk),metadata={"status":status,"note":note})
    messages.success(request,"Correction review saved."); return redirect("time_review")

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.PAYROLL)
def payroll(request):
    form=PayrollPeriodForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        run=create_payroll_run(organization=request.organization,start=form.cleaned_data["period_start"],end=form.cleaned_data["period_end"],actor=request.user)
        messages.success(request,"Payroll draft generated."); return redirect("payroll")
    return render(request,"core/payroll.html",{"form":form,"runs":request.organization.payroll_runs.all()[:30]})

@require_POST
@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.PAYROLL)
def payroll_approve(request,run_id):
    run=request.organization.payroll_runs.filter(pk=run_id).first()
    if not run: raise Http404
    try: approve_payroll_run(run,request.user)
    except ValidationError as exc: messages.error(request,str(exc))
    else: messages.success(request,"Payroll approved and locked.")
    return redirect("payroll")

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.PAYROLL)
@transaction.atomic
def payroll_run_export(request,run_id):
    run=request.organization.payroll_runs.filter(pk=run_id,status__in=[PayrollRun.Status.APPROVED,PayrollRun.Status.EXPORTED]).first()
    if not run: raise Http404
    format=request.GET.get("format","csv").lower()
    exporters={"csv":(payroll_snapshot_csv,"text/csv"),"xlsx":(payroll_snapshot_xlsx,"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),"pdf":(payroll_snapshot_pdf,"application/pdf")}
    if format not in exporters: raise Http404
    exporter,content_type=exporters[format];output=exporter(run.snapshot)
    run.status=PayrollRun.Status.EXPORTED;run.exported_at=timezone.now();run.save(update_fields=["status","exported_at"])
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="payroll.exported",target_type="payroll_run",target_id=str(run.pk),metadata={"rows":len(run.snapshot),"format":format})
    response=HttpResponse(output,content_type=content_type);response["Content-Disposition"]=f'attachment; filename="payroll-{run.period_start.date()}-{run.period_end.date()}.{format}"';return response

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def security_settings(request):
    form=OrganizationSecurityForm(request.POST or None,instance=request.organization)
    if request.method=="POST" and form.is_valid():
        before=request.organization.mfa_required_roles;form.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="security.mfa_policy_updated",target_type="organization",target_id=str(request.organization.pk),metadata={"before":before,"after":request.organization.mfa_required_roles})
        messages.success(request,"MFA policy updated.");return redirect("security_settings")
    return render(request,"core/security_settings.html",{"form":form})

@membership_required()
def tenant_select(request):
    memberships=request.user.organization_memberships.filter(active=True).select_related("organization")
    if request.method=="POST":
        membership=memberships.filter(organization_id=request.POST.get("organization_id")).first()
        if not membership: raise Http404
        request.session.cycle_key();request.session["active_organization_id"]=str(membership.organization_id)
        AuditEvent.objects.create(organization=membership.organization,actor=request.user,action="tenant.selected",target_type="organization",target_id=str(membership.organization_id))
        return redirect("dashboard")
    return render(request,"core/tenant_select.html",{"memberships":memberships})

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def domains(request):
    import secrets
    form=DomainForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        item,created=OrganizationDomain.objects.get_or_create(hostname=form.cleaned_data["hostname"],defaults={"organization":request.organization,"verification_token":secrets.token_hex(24)})
        if not created and item.organization_id!=request.organization.id: form.add_error("hostname","This hostname is already claimed.")
        else:
            AuditEvent.objects.create(organization=request.organization,actor=request.user,action="domain.requested",target_type="organization_domain",target_id=str(item.pk),metadata={"hostname":item.hostname})
            messages.success(request,"Domain added. Publish the displayed DNS TXT record, then verify.");return redirect("domains")
    return render(request,"core/domains.html",{"form":form,"domains":request.organization.domains.all()})

@require_POST
@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def domain_verify(request,domain_id):
    import dns.resolver
    item=request.organization.domains.filter(pk=domain_id).first()
    if not item: raise Http404
    try:
        answers=dns.resolver.resolve(f"_tscm-verification.{item.hostname}","TXT")
        values={part.decode() for answer in answers for part in answer.strings}
        if item.verification_token not in values: raise ValueError("Verification token was not found.")
    except Exception:
        item.status=OrganizationDomain.Status.FAILED;item.save(update_fields=["status"]);messages.error(request,"DNS verification failed. Confirm the TXT record and try again.")
    else:
        item.verified=True;item.status=OrganizationDomain.Status.VERIFIED;item.verified_at=timezone.now();item.save(update_fields=["verified","status","verified_at"])
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="domain.verified",target_type="organization_domain",target_id=str(item.pk),metadata={"hostname":item.hostname});messages.success(request,"Domain verified. Configure it in the deployment platform to provision TLS.")
    return redirect("domains")

@login_required
def platform_admin(request):
    if not request.user.is_superuser: raise Http404
    organizations=Organization.objects.annotate(member_count=__import__("django.db.models",fromlist=["Count"]).Count("memberships"),people_count=__import__("django.db.models",fromlist=["Count"]).Count("people",distinct=True)).order_by("display_name")
    return render(request,"core/platform_admin.html",{"organizations":organizations})

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.AUDITOR)
def audit_log(request):
    events=request.organization.audit_events.select_related("actor").prefetch_related("redactions")[:500]
    return render(request,"core/audit_log.html",{"events":events,"chain_errors":verify_audit_chain(request.organization)})

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.AUDITOR)
def audit_export(request):
    import json
    errors=verify_audit_chain(request.organization)
    response=HttpResponse(content_type="application/x-ndjson");response["Content-Disposition"]='attachment; filename="audit-export.ndjson"'
    for event in request.organization.audit_events.order_by("occurred_at","id").prefetch_related("redactions"):
        metadata=dict(event.metadata);redaction=event.redactions.first()
        if redaction:
            for field in redaction.fields:
                if field in metadata:metadata[field]="[REDACTED]"
        row={"id":str(event.pk),"occurred_at":event.occurred_at.isoformat(),"actor_id":event.actor_id,"action":event.action,"target_type":event.target_type,"target_id":event.target_id,"metadata":metadata,"previous_hash":event.previous_hash,"event_hash":event.event_hash,"redacted":bool(redaction),"chain_verified":str(event.pk) not in errors}
        response.write(json.dumps(row,sort_keys=True,default=str)+"\n")
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="audit.exported",target_type="organization",target_id=str(request.organization.pk),metadata={"event_count":request.organization.audit_events.count(),"chain_errors":len(errors)})
    response["Cache-Control"]="private, no-store";return response

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def audit_redact(request,event_id):
    event=request.organization.audit_events.filter(pk=event_id).first()
    if not event:raise Http404
    form=AuditRedactionForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        redaction=AuditRedaction.objects.create(organization=request.organization,event=event,fields=form.cleaned_data["fields"],reason=form.cleaned_data["reason"],legal_basis=form.cleaned_data["legal_basis"],requested_by=request.user,approved_by=request.user)
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="audit.redaction_recorded",target_type="audit_redaction",target_id=str(redaction.pk),metadata={"event_id":str(event.pk),"fields":redaction.fields,"reason":redaction.reason,"legal_basis":redaction.legal_basis})
        messages.success(request,"Export redaction tombstone recorded.");return redirect("audit_log")
    return render(request,"core/form.html",{"form":form,"title":"Redact audit export fields","eyebrow":"Audit governance"})

@never_cache
def manifest(request):
    membership = request.user.organization_memberships.filter(active=True).select_related("organization").first() if request.user.is_authenticated else None
    name = membership.organization.display_name if membership else "Texas Security Company Manager"
    return JsonResponse({"name": name, "short_name": name[:24], "start_url": "/", "display": "standalone", "background_color": "#F4F7FB", "theme_color": membership.organization.primary_color if membership else "#16324F", "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})

def service_worker(request):
    js = '''const CACHE="tscm-shell-v1";const SHELL=["/static/css/app.css","/static/js/app.js","/static/icon.svg"];
self.addEventListener("install",e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL))));
self.addEventListener("activate",e=>e.waitUntil(self.clients.claim()));
self.addEventListener("fetch",e=>{if(e.request.method!=="GET")return;e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));});'''
    return HttpResponse(js, content_type="application/javascript", headers={"Service-Worker-Allowed": "/"})
