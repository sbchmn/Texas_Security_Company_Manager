import csv
import uuid
import math
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from io import StringIO
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .models import AuditEvent, Credential, Punch, Shift, TimePolicy

INVALID_CREDENTIAL_STATES = {"missing", "pending", "unverified", "expired", "suspended", "revoked"}

def shift_eligibility(shift, officer=None):
    officer = officer or shift.officer
    reasons=[]
    if not officer:
        return False,["No officer is assigned."]
    if officer.organization_id != shift.organization_id:
        return False,["Officer belongs to another organization."]
    credentials={c.credential_type_id:c for c in officer.credentials.select_related("credential_type")}
    for required in shift.required_credentials.all():
        credential=credentials.get(required.id)
        state=credential.effective_status if credential else "missing"
        if state in INVALID_CREDENTIAL_STATES:
            reasons.append(f"{required.name}: {state}.")
    overlap=officer.shifts.exclude(pk=shift.pk).exclude(status=Shift.Status.CANCELLED).filter(starts_at__lt=shift.ends_at,ends_at__gt=shift.starts_at).exists()
    if overlap: reasons.append("Officer has an overlapping shift.")
    return not reasons,reasons

def haversine_meters(lat1, lon1, lat2, lon2):
    radius=6371000
    p1,p2=math.radians(float(lat1)),math.radians(float(lat2))
    dp=math.radians(float(lat2)-float(lat1)); dl=math.radians(float(lon2)-float(lon1))
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return radius*2*math.atan2(math.sqrt(a),math.sqrt(1-a))

@transaction.atomic
def record_punch(*, organization, person, client_event_id, kind, occurred_at, actor=None, shift=None, latitude=None, longitude=None, offline=False, source="web"):
    from .models import PayrollRun
    if PayrollRun.objects.filter(organization=organization,status__in=[PayrollRun.Status.APPROVED,PayrollRun.Status.EXPORTED],period_start__lte=occurred_at,period_end__gt=occurred_at).exists():
        raise ValidationError("This payroll period is locked.")
    existing=Punch.objects.filter(client_event_id=client_event_id).first()
    if existing:
        if existing.organization_id != organization.id or existing.person_id != person.id:
            raise ValidationError("Client event ID is already in use.")
        return existing,False
    if person.organization_id != organization.id:
        raise ValidationError("Person belongs to another organization.")
    if timezone.now()-occurred_at > timedelta(hours=12):
        raise ValidationError("Offline punches must synchronize within 12 hours.")
    policy,_=TimePolicy.objects.get_or_create(organization=organization,defaults={"timezone":organization.timezone})
    review=Punch.Review.ACCEPTED; reason=""
    if kind == Punch.Kind.IN and shift:
        allowed,reasons=shift_eligibility(shift,person)
        if not allowed: raise ValidationError("Clock-in blocked: "+" ".join(reasons))
    if policy.require_geofence and shift and shift.site.latitude is not None and shift.site.longitude is not None:
        if latitude is None or longitude is None:
            review=Punch.Review.PENDING; reason="Location was not supplied."
        elif haversine_meters(latitude,longitude,shift.site.latitude,shift.site.longitude)>shift.site.geofence_radius_meters:
            review=Punch.Review.PENDING; reason="Punch was outside the site geofence."
    punch=Punch.objects.create(organization=organization,person=person,shift=shift,client_event_id=client_event_id,kind=kind,occurred_at=occurred_at,latitude=latitude,longitude=longitude,offline=offline,review_status=review,exception_reason=reason,source=source)
    AuditEvent.objects.create(organization=organization,actor=actor,action="punch.recorded",target_type="punch",target_id=str(punch.pk),metadata={"kind":kind,"offline":offline,"review":review})
    return punch,True

def round_minutes(minutes, policy):
    if policy.rounding_mode == TimePolicy.RoundingMode.EXACT or policy.rounding_minutes == 1:
        return minutes
    interval=Decimal(policy.rounding_minutes); value=Decimal(minutes)/interval
    modes={TimePolicy.RoundingMode.NEAREST:ROUND_HALF_UP,TimePolicy.RoundingMode.UP:ROUND_CEILING,TimePolicy.RoundingMode.DOWN:ROUND_FLOOR}
    return int(value.quantize(Decimal("1"),rounding=modes[policy.rounding_mode])*interval)

def payroll_rows(organization, start, end):
    policy,_=TimePolicy.objects.get_or_create(organization=organization,defaults={"timezone":organization.timezone})
    punches=organization.punches.filter(occurred_at__gte=start,occurred_at__lt=end,review_status=Punch.Review.ACCEPTED).select_related("person","shift__site__client").prefetch_related("adjustments")
    grouped=defaultdict(list)
    for punch in punches: grouped[punch.person].append(punch)
    rows=[]
    for person,events in grouped.items():
        total=0; open_in=None
        for event in events:
            approved=next((item for item in event.adjustments.all() if item.status=="approved"),None)
            event_time=approved.proposed_at if approved else event.occurred_at
            if event.kind==Punch.Kind.IN: open_in=event
            elif event.kind==Punch.Kind.OUT and open_in:
                in_adjustment=next((item for item in open_in.adjustments.all() if item.status=="approved"),None)
                in_time=in_adjustment.proposed_at if in_adjustment else open_in.occurred_at
                if event_time>=in_time: total += int((event_time-in_time).total_seconds()//60); open_in=None
        rounded=round_minutes(total,policy); hours=Decimal(rounded)/Decimal(60); regular=min(hours,policy.overtime_after_hours); overtime=max(Decimal(0),hours-policy.overtime_after_hours)
        rows.append({"employee_id":str(person.pk),"employee":person.full_name,"regular_hours":regular.quantize(Decimal("0.01")),"overtime_hours":overtime.quantize(Decimal("0.01")),"total_hours":hours.quantize(Decimal("0.01")),"exception":"Open punch" if open_in else ""})
    return rows

@transaction.atomic
def create_payroll_run(*,organization,start,end,actor):
    from .models import AuditEvent, PayrollRun, Punch
    rows=payroll_rows(organization,start,end)
    exceptions=[{"employee":row["employee"],"reason":row["exception"]} for row in rows if row["exception"]]
    pending=organization.punches.filter(occurred_at__gte=start,occurred_at__lt=end,review_status=Punch.Review.PENDING).count()
    if pending: exceptions.append({"employee":"Multiple","reason":f"{pending} punches await review"})
    serializable=[{key:str(value) if isinstance(value,Decimal) else value for key,value in row.items()} for row in rows]
    run,created=PayrollRun.objects.get_or_create(organization=organization,period_start=start,period_end=end,defaults={"created_by":actor,"snapshot":serializable,"exceptions":exceptions})
    if not created and run.status==PayrollRun.Status.DRAFT:
        run.snapshot=serializable;run.exceptions=exceptions;run.save(update_fields=["snapshot","exceptions"])
    AuditEvent.objects.create(organization=organization,actor=actor,action="payroll.generated",target_type="payroll_run",target_id=str(run.pk),metadata={"exceptions":len(exceptions)})
    return run

@transaction.atomic
def approve_payroll_run(run,actor):
    from .models import AuditEvent, PayrollRun
    if run.status!=PayrollRun.Status.DRAFT: raise ValidationError("Only draft payroll runs can be approved.")
    if run.exceptions: raise ValidationError("Resolve payroll exceptions before approval.")
    run.status=PayrollRun.Status.APPROVED;run.approved_by=actor;run.approved_at=timezone.now();run.save(update_fields=["status","approved_by","approved_at"])
    AuditEvent.objects.create(organization=run.organization,actor=actor,action="payroll.approved",target_type="payroll_run",target_id=str(run.pk),metadata={"rows":len(run.snapshot)})
    return run

def payroll_csv(organization,start,end):
    output=StringIO(); fields=["employee_id","employee","regular_hours","overtime_hours","total_hours","exception"]
    writer=csv.DictWriter(output,fieldnames=fields); writer.writeheader(); writer.writerows(payroll_rows(organization,start,end)); return output.getvalue()

ALLOWED_DOCUMENT_SIGNATURES = {
    ".pdf": (b"%PDF-",), ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
    ".docx": (b"PK\x03\x04",), ".xlsx": (b"PK\x03\x04",),
    ".csv": tuple(), ".txt": tuple(),
}

def validate_document_upload(upload):
    from pathlib import Path
    if upload.size > 25*1024*1024: raise ValidationError("Documents must be 25 MiB or smaller.")
    extension=Path(upload.name).suffix.lower()
    if extension not in ALLOWED_DOCUMENT_SIGNATURES: raise ValidationError("Unsupported document type.")
    head=upload.read(16); upload.seek(0)
    signatures=ALLOWED_DOCUMENT_SIGNATURES[extension]
    if signatures and not any(head.startswith(signature) for signature in signatures):
        raise ValidationError("File contents do not match the extension.")
    if b"<script" in upload.read(4096).lower(): upload.seek(0); raise ValidationError("Active content is not allowed.")
    upload.seek(0)

def malware_scan(upload):
    import socket
    from django.conf import settings
    upload.seek(0)
    if settings.MALWARE_SCAN_MODE == "basic":
        infected=b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in upload.read()
        upload.seek(0)
        return not infected,"EICAR test signature" if infected else "basic validation"
    if not settings.CLAMAV_HOST: raise ValidationError("Malware scanner is unavailable.")
    with socket.create_connection((settings.CLAMAV_HOST,settings.CLAMAV_PORT),timeout=30) as sock:
        sock.sendall(b"zINSTREAM\0")
        while chunk:=upload.read(65536): sock.sendall(len(chunk).to_bytes(4,"big")+chunk)
        sock.sendall((0).to_bytes(4,"big")); result=sock.recv(4096).decode("utf-8","replace")
    upload.seek(0)
    return result.endswith("OK\0"),result.rstrip("\0")

@transaction.atomic
def store_person_document(*, organization, person, document_type, upload, actor, expires_on=None):
    import hashlib
    from datetime import timedelta
    from .models import AuditEvent, PersonDocument
    validate_document_upload(upload)
    clean,scan_detail=malware_scan(upload)
    digest=hashlib.sha256()
    for chunk in upload.chunks(): digest.update(chunk)
    upload.seek(0)
    retain_until=None
    if document_type.retention_days is not None: retain_until=timezone.localdate()+timedelta(days=document_type.retention_days)
    document=PersonDocument.objects.create(organization=organization,person=person,document_type=document_type,file=upload,original_name=upload.name,content_type=getattr(upload,"content_type","")[:100],size=upload.size,sha256=digest.hexdigest(),scan_status=PersonDocument.ScanStatus.CLEAN if clean else PersonDocument.ScanStatus.REJECTED,expires_on=expires_on,retain_until=retain_until,uploaded_by=actor)
    AuditEvent.objects.create(organization=organization,actor=actor,action="document.uploaded",target_type="person_document",target_id=str(document.pk),metadata={"type":document_type.code,"size":upload.size,"sha256":document.sha256,"scan":scan_detail})
    if not clean: document.file.delete(save=False); raise ValidationError("The upload failed malware scanning.")
    return document

def _provider_secret(name):
    from django.conf import settings
    value=getattr(settings,name,"")
    if not value: raise RuntimeError(f"{name} is not configured")
    return value

def deliver_notification(notification):
    """Deliver one queued email/SMS. In-app notices become immediately visible."""
    import boto3
    import requests
    from django.conf import settings
    from .models import Notification, Organization
    if notification.status not in (Notification.Status.QUEUED,Notification.Status.FAILED): return notification
    notification.attempts += 1
    try:
        if notification.channel == Notification.Channel.IN_APP:
            pass
        elif notification.channel == Notification.Channel.EMAIL:
            recipient=notification.recipient.email
            if not recipient: raise RuntimeError("Recipient has no email address")
            sender=notification.organization.email_from or settings.DEFAULT_FROM_EMAIL
            provider=notification.organization.email_provider
            if provider==Organization.EmailProvider.MAILJET:
                response=requests.post("https://api.mailjet.com/v3.1/send",auth=(_provider_secret("MAILJET_API_KEY"),_provider_secret("MAILJET_SECRET_KEY")),json={"Messages":[{"From":{"Email":sender,"Name":notification.organization.display_name},"To":[{"Email":recipient}],"Subject":notification.subject,"TextPart":notification.body,"HTMLPart":branded_email_html(notification)}]},timeout=15); response.raise_for_status()
            elif provider==Organization.EmailProvider.POSTMARK:
                response=requests.post("https://api.postmarkapp.com/email",headers={"X-Postmark-Server-Token":_provider_secret("POSTMARK_SERVER_TOKEN")},json={"From":sender,"To":recipient,"Subject":notification.subject,"TextBody":notification.body,"HtmlBody":branded_email_html(notification)},timeout=15); response.raise_for_status()
            elif provider==Organization.EmailProvider.SES:
                boto3.client("ses",region_name=settings.AWS_REGION).send_email(Source=sender,Destination={"ToAddresses":[recipient]},Message={"Subject":{"Data":notification.subject},"Body":{"Text":{"Data":notification.body},"Html":{"Data":branded_email_html(notification)}}})
        elif notification.channel == Notification.Channel.SMS:
            person=notification.organization.people.filter(user=notification.recipient).first()
            destination=getattr(person,"mobile_phone","")
            if not destination: raise RuntimeError("Recipient has no mobile phone")
            if notification.organization.sms_provider==Organization.SmsProvider.SNS:
                boto3.client("sns",region_name=settings.AWS_REGION).publish(PhoneNumber=destination,Message=notification.body)
            else:
                sid=_provider_secret("TWILIO_ACCOUNT_SID"); token=_provider_secret("TWILIO_AUTH_TOKEN")
                response=requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",auth=(sid,token),data={"To":destination,"From":notification.organization.sms_from or _provider_secret("TWILIO_FROM_NUMBER"),"Body":notification.body},timeout=15); response.raise_for_status()
        notification.status=Notification.Status.SENT; notification.sent_at=timezone.now(); notification.last_error=""
    except Exception as exc:
        notification.status=Notification.Status.FAILED; notification.last_error=str(exc)[:500]
    notification.save(update_fields=["attempts","status","sent_at","last_error"])
    return notification

def queue_compliance_reminders(today=None):
    from .models import Credential, Membership, Notification
    today=today or timezone.localdate(); created=0
    credentials=Credential.objects.select_related("organization","person","person__user","credential_type").filter(organization__isnull=False)
    for credential in credentials:
        state=credential.effective_status; days=(credential.expires_on-today).days if credential.expires_on else None
        due=state in INVALID_CREDENTIAL_STATES or (days is not None and 0 <= days <= credential.credential_type.warning_days)
        if not due: continue
        recipients=set(credential.organization.memberships.filter(active=True,role__in=[Membership.Role.OWNER,Membership.Role.ADMIN,Membership.Role.HR]).values_list("user_id",flat=True))
        if credential.person.user_id: recipients.add(credential.person.user_id)
        state_label=state if state!="active" else f"expires in {days} days"
        for user_id in recipients:
            for channel in (Notification.Channel.IN_APP,Notification.Channel.EMAIL):
                _,was_created=Notification.objects.get_or_create(organization=credential.organization,recipient_id=user_id,channel=channel,deduplication_key=f"credential:{credential.pk}:{state}:{today.isoformat()}",defaults={"event_type":"credential.reminder","subject":f"Credential attention: {credential.credential_type.name}","body":f"{credential.person.full_name}'s {credential.credential_type.name} {state_label}."})
                created += int(was_created)
    return created

IMPORT_COLUMNS={
 "branches":{"name"}, "people":{"first_name","last_name","email"}, "clients":{"name"},
 "sites":{"client","name","address"}, "credentials":{"person_email","type_code","status"},
 "training":{"person_email","course_name","completed_on"}, "shifts":{"client","site","starts_at","ends_at"},
}

def preview_csv_import(*,organization,entity,upload,actor):
    import hashlib
    from .models import ImportBatch
    if upload.size>50*1024*1024: raise ValidationError("CSV imports must be 50 MiB or smaller.")
    raw=upload.read(); digest=hashlib.sha256(raw).hexdigest()
    try: text=raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc: raise ValidationError("CSV must be UTF-8 encoded.") from exc
    reader=csv.DictReader(StringIO(text)); fields=set(reader.fieldnames or []); required=IMPORT_COLUMNS.get(entity)
    if required is None: raise ValidationError("Unsupported import type.")
    missing=required-fields
    if missing: raise ValidationError("Missing columns: "+", ".join(sorted(missing)))
    rows=[]; errors=[]
    for number,row in enumerate(reader,start=2):
        cleaned={str(k).strip():str(v or "").strip() for k,v in row.items()}
        row_errors=[]
        for field in required:
            if not cleaned.get(field): row_errors.append(f"{field} is required")
        if entity in ("training",) and cleaned.get("completed_on"):
            try: __import__("datetime").date.fromisoformat(cleaned["completed_on"])
            except ValueError: row_errors.append("completed_on must use YYYY-MM-DD")
        if entity=="shifts":
            for field in ("starts_at","ends_at"):
                try: datetime.fromisoformat(cleaned[field].replace("Z","+00:00"))
                except ValueError: row_errors.append(f"{field} must be ISO-8601")
        rows.append(cleaned)
        if row_errors: errors.append({"row":number,"errors":row_errors})
    if len(rows)>10000: raise ValidationError("A single import may contain at most 10,000 rows.")
    batch,created=ImportBatch.objects.get_or_create(organization=organization,entity=entity,source_hash=digest,defaults={"source_name":upload.name,"rows":rows,"errors":errors,"status":ImportBatch.Status.INVALID if errors else ImportBatch.Status.PREVIEW,"created_by":actor})
    return batch,created

def _bool(value): return str(value).casefold() in ("1","true","yes","y")

@transaction.atomic
def apply_csv_import(batch,actor):
    from .models import AuditEvent, Branch, Client, Credential, CredentialType, ImportBatch, Person, Shift, Site, TrainingRecord
    if batch.status==ImportBatch.Status.APPLIED: return 0
    if batch.errors: raise ValidationError("Correct validation errors before applying this import.")
    org=batch.organization; count=0
    for row in batch.rows:
        if batch.entity==ImportBatch.Entity.BRANCHES:
            Branch.objects.update_or_create(organization=org,name=row["name"],defaults={"city":row.get("city","")})
        elif batch.entity==ImportBatch.Entity.PEOPLE:
            branch=org.branches.filter(name=row.get("branch","")).first()
            defaults={"first_name":row["first_name"],"last_name":row["last_name"],"branch":branch,"status":row.get("status") or Person.Status.ONBOARDING,"is_unarmed_officer":_bool(row.get("is_unarmed_officer")),"is_commissioned_officer":_bool(row.get("is_commissioned_officer")),"is_ppo":_bool(row.get("is_ppo")),"is_private_investigator":_bool(row.get("is_private_investigator")),"is_shareholder":_bool(row.get("is_shareholder"))}
            for field in ("employee_id","job_title","mobile_phone","address_line1","address_line2","city","state","postal_code","emergency_contact_name","emergency_contact_phone","hourly_rate","hire_date","termination_date","date_of_birth"):
                if row.get(field):defaults[field]=row[field]
            Person.objects.update_or_create(organization=org,email=row["email"],defaults=defaults)
        elif batch.entity==ImportBatch.Entity.CLIENTS:
            Client.objects.update_or_create(organization=org,name=row["name"],defaults={"contact_name":row.get("contact_name", ""),"contact_email":row.get("contact_email","")})
        elif batch.entity==ImportBatch.Entity.SITES:
            client=org.clients.get(name=row["client"]); branch=org.branches.filter(name=row.get("branch","")).first()
            Site.objects.update_or_create(organization=org,client=client,name=row["name"],defaults={"branch":branch,"address":row["address"],"latitude":row.get("latitude") or None,"longitude":row.get("longitude") or None,"geofence_radius_meters":int(row.get("geofence_radius_meters") or 200)})
        elif batch.entity==ImportBatch.Entity.CREDENTIALS:
            person=org.people.get(email=row["person_email"]); kind=org.credential_types.get(code=row["type_code"])
            Credential.objects.update_or_create(organization=org,person=person,credential_type=kind,number=row.get("number",""),defaults={"status":row["status"],"issued_on":row.get("issued_on") or None,"expires_on":row.get("expires_on") or None})
        elif batch.entity==ImportBatch.Entity.TRAINING:
            person=org.people.get(email=row["person_email"])
            TrainingRecord.objects.update_or_create(organization=org,person=person,course_name=row["course_name"],completed_on=row["completed_on"],defaults={"provider":row.get("provider",""),"expires_on":row.get("expires_on") or None,"certificate_number":row.get("certificate_number","")})
        elif batch.entity==ImportBatch.Entity.SHIFTS:
            site=org.sites.get(client__name=row["client"],name=row["site"]); officer=org.people.filter(email=row.get("officer_email","")).first()
            Shift.objects.update_or_create(organization=org,site=site,starts_at=datetime.fromisoformat(row["starts_at"].replace("Z","+00:00")),ends_at=datetime.fromisoformat(row["ends_at"].replace("Z","+00:00")),defaults={"officer":officer,"post_name":row.get("post_name", ""),"post_orders":row.get("post_orders",""),"status":row.get("status") or Shift.Status.DRAFT})
        count+=1
    batch.status=ImportBatch.Status.APPLIED; batch.applied_at=timezone.now(); batch.save(update_fields=["status","applied_at"])
    AuditEvent.objects.create(organization=org,actor=actor,action="import.applied",target_type="import_batch",target_id=str(batch.pk),metadata={"entity":batch.entity,"rows":count,"hash":batch.source_hash})
    return count

@transaction.atomic
def execute_disposition(request,actor):
    from .models import AuditEvent, DispositionRequest
    document=request.document
    if request.status!=DispositionRequest.Status.PENDING: raise ValidationError("Disposition request is not pending.")
    if document.legal_hold: raise ValidationError("Document is under legal hold.")
    now=timezone.now()
    if request.action==DispositionRequest.Action.ARCHIVE:
        document.archived_at=now;document.save(update_fields=["archived_at"])
    else:
        storage_name=document.file.name
        if storage_name: document.file.storage.delete(storage_name)
        document.file="";document.deleted_at=now;document.save(update_fields=["file","deleted_at"])
    request.status=DispositionRequest.Status.EXECUTED;request.approved_by=actor;request.executed_at=now;request.save(update_fields=["status","approved_by","executed_at"])
    AuditEvent.objects.create(organization=request.organization,actor=actor,action=f"document.{request.action}d",target_type="person_document_tombstone",target_id=str(document.pk),metadata={"request":str(request.pk),"reason":request.reason,"original_name":document.original_name,"sha256":document.sha256,"person":str(document.person_id),"document_type":str(document.document_type_id)})
    return document

def process_brand_image(upload):
    from io import BytesIO
    from django.core.files.base import ContentFile
    from PIL import Image,ImageOps,UnidentifiedImageError
    if upload.size>5*1024*1024: raise ValidationError("Brand images must be 5 MiB or smaller.")
    clean,_=malware_scan(upload)
    if not clean: raise ValidationError("The brand image failed malware scanning.")
    try:
        image=Image.open(upload);image.verify();upload.seek(0);image=Image.open(upload);image=ImageOps.exif_transpose(image).convert("RGBA")
    except (UnidentifiedImageError,Image.DecompressionBombError,Image.DecompressionBombWarning) as exc: raise ValidationError("Upload a safe PNG, JPEG, or WebP image.") from exc
    image.thumbnail((2048,2048));output=BytesIO();image.save(output,format="PNG",optimize=True)
    return ContentFile(output.getvalue(),name=f"brand-{uuid.uuid4().hex}.png")

def brand_snapshot(organization):
    return {"display_name":organization.display_name,"primary_color":organization.primary_color,"accent_color":organization.accent_color,"dark_primary_color":organization.dark_primary_color,"dark_accent_color":organization.dark_accent_color,"dark_mode_enabled":organization.dark_mode_enabled,"logo":organization.logo.name if organization.logo else "","support_email":organization.support_email,"support_phone":organization.support_phone,"timezone":organization.timezone}

@transaction.atomic
def create_brand_version(organization,actor):
    from django.db.models import Max
    from .models import BrandVersion
    version=(organization.brand_versions.aggregate(value=Max("version"))["value"] or 0)+1
    return BrandVersion.objects.create(organization=organization,version=version,snapshot=brand_snapshot(organization),created_by=actor)

def branded_email_html(notification):
    import html
    org=notification.organization
    logo=f'<img src="{html.escape(org.logo.url)}" alt="{html.escape(org.display_name)}" style="max-height:56px">' if org.logo else ""
    return f'<div style="font-family:Arial,sans-serif;color:#172033"><div style="border-bottom:4px solid {org.accent_color};padding:16px 0">{logo}<strong>{html.escape(org.display_name)}</strong></div><h1 style="color:{org.primary_color};font-size:22px">{html.escape(notification.subject)}</h1><p>{html.escape(notification.body).replace(chr(10),"<br>")}</p></div>'

def person_snapshot(person):
    fields=("employee_id","first_name","last_name","email","mobile_phone","job_title","hire_date","termination_date","date_of_birth","address_line1","address_line2","city","state","postal_code","emergency_contact_name","emergency_contact_phone","hourly_rate","status","is_unarmed_officer","is_commissioned_officer","is_ppo","is_private_investigator","is_shareholder","branch_id")
    return {field:str(getattr(person,field)) if getattr(person,field) is not None else None for field in fields}

def record_person_history(person,before,actor):
    from .models import AuditEvent,PersonHistory
    after=person_snapshot(person);changes={key:{"before":before.get(key),"after":value} for key,value in after.items() if before.get(key)!=value}
    if changes:
        PersonHistory.objects.create(person=person,organization=person.organization,changed_by=actor,changes=changes,snapshot=after)
        AuditEvent.objects.create(organization=person.organization,actor=actor,action="person.updated",target_type="person",target_id=str(person.pk),metadata={"fields":sorted(changes)})
    return changes

def coerce_custom_value(definition,raw):
    from .models import CustomFieldDefinition
    if raw in (None,""):
        if definition.required: raise ValidationError(f"{definition.name} is required.")
        return None
    if definition.kind==CustomFieldDefinition.Kind.NUMBER:
        try:return str(Decimal(raw))
        except Exception as exc:raise ValidationError(f"{definition.name} must be a number.") from exc
    if definition.kind==CustomFieldDefinition.Kind.DATE:
        try:return __import__("datetime").date.fromisoformat(raw).isoformat()
        except ValueError as exc:raise ValidationError(f"{definition.name} must be a date.") from exc
    if definition.kind==CustomFieldDefinition.Kind.BOOLEAN:return str(raw).casefold() in ("1","true","yes","on")
    return str(raw)[:2000]

def verify_audit_chain(organization):
    import hashlib,json
    previous="";errors=[]
    for event in organization.audit_events.order_by("occurred_at","id"):
        payload=json.dumps({"id":str(event.pk),"organization":str(event.organization_id),"actor":event.actor_id,"action":event.action,"target_type":event.target_type,"target_id":event.target_id,"metadata":event.metadata,"previous_hash":previous},sort_keys=True,separators=(",",":"),default=str)
        expected=hashlib.sha256(payload.encode()).hexdigest()
        if event.previous_hash!=previous or event.event_hash!=expected:errors.append(str(event.pk))
        previous=event.event_hash
    return errors
