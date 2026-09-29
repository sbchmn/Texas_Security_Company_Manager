import json
import uuid
from datetime import datetime, timedelta
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from .auth import membership_required
from .forms import BrandForm, BranchForm, ClientForm, CredentialForm, CredentialTypeForm, CsvImportForm, DispositionRequestForm, DocumentTypeForm, DocumentUploadForm, OrganizationSecurityForm, PayrollPeriodForm, PersonForm, PunchAdjustmentForm, ShiftForm, SiteForm, TimePolicyForm, TrainingRecordForm
from .models import AuditEvent, Client, Credential, CredentialType, DispositionRequest, DocumentType, ImportBatch, Membership, Notification, PayrollRun, Person, PersonDocument, Punch, PunchAdjustment, Shift, Site, TimePolicy, TrainingRecord
from .services import apply_csv_import, approve_payroll_run, create_payroll_run, execute_disposition, payroll_csv, preview_csv_import, record_punch, shift_eligibility, store_person_document

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
    css=f":root{{--brand:{request.organization.primary_color};--accent:{request.organization.accent_color}}}"
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
        form.save()
        after = {"display_name": org.display_name, "primary_color": org.primary_color, "accent_color": org.accent_color}
        AuditEvent.objects.create(organization=org, actor=request.user, action="branding.updated", target_type="organization", target_id=str(org.pk), metadata={"before": before, "after": after})
        messages.success(request, "Brand settings published.")
        return redirect("branding")
    return render(request, "core/branding.html", {"form": form})

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
        item=form.save(commit=False); item.organization=request.organization; item.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="credential_type.created",target_type="credential_type",target_id=str(item.pk),metadata={"name":item.name})
        messages.success(request,"Credential type created."); return redirect("compliance")
    return render(request,"core/form.html",{"form":form,"title":"Add credential type","eyebrow":"Compliance"})

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

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.HR,Membership.Role.AUDITOR)
def document_download(request,document_id):
    document=request.organization.person_documents.filter(pk=document_id,deleted_at__isnull=True,scan_status=PersonDocument.ScanStatus.CLEAN).first()
    if not document: raise Http404
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="document.downloaded",target_type="person_document",target_id=str(document.pk))
    response=FileResponse(document.file.open("rb"),as_attachment=True,filename=document.original_name,content_type="application/octet-stream")
    response["X-Content-Type-Options"]="nosniff"; response["Cache-Control"]="private, no-store"; return response

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
    import csv
    from io import StringIO
    run=request.organization.payroll_runs.filter(pk=run_id,status__in=[PayrollRun.Status.APPROVED,PayrollRun.Status.EXPORTED]).first()
    if not run: raise Http404
    fields=["employee_id","employee","regular_hours","overtime_hours","total_hours","exception"]; output=StringIO();writer=csv.DictWriter(output,fieldnames=fields);writer.writeheader();writer.writerows(run.snapshot)
    run.status=PayrollRun.Status.EXPORTED;run.exported_at=timezone.now();run.save(update_fields=["status","exported_at"])
    AuditEvent.objects.create(organization=request.organization,actor=request.user,action="payroll.exported",target_type="payroll_run",target_id=str(run.pk),metadata={"rows":len(run.snapshot)})
    response=HttpResponse(output.getvalue(),content_type="text/csv");response["Content-Disposition"]=f'attachment; filename="payroll-{run.period_start.date()}-{run.period_end.date()}.csv"';return response

@membership_required(Membership.Role.OWNER,Membership.Role.ADMIN)
def security_settings(request):
    form=OrganizationSecurityForm(request.POST or None,instance=request.organization)
    if request.method=="POST" and form.is_valid():
        before=request.organization.mfa_required_roles;form.save()
        AuditEvent.objects.create(organization=request.organization,actor=request.user,action="security.mfa_policy_updated",target_type="organization",target_id=str(request.organization.pk),metadata={"before":before,"after":request.organization.mfa_required_roles})
        messages.success(request,"MFA policy updated.");return redirect("security_settings")
    return render(request,"core/security_settings.html",{"form":form})

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
