import hashlib,json
from django.db import migrations

def forward(apps,schema_editor):
    Audit=apps.get_model("core","AuditEvent")
    organizations=Audit.objects.order_by().values_list("organization_id",flat=True).distinct()
    for organization_id in organizations:
        previous=""
        for event in Audit.objects.filter(organization_id=organization_id).order_by("occurred_at","id"):
            payload=json.dumps({"id":str(event.pk),"organization":str(event.organization_id),"actor":event.actor_id,"action":event.action,"target_type":event.target_type,"target_id":event.target_id,"metadata":event.metadata,"previous_hash":previous},sort_keys=True,separators=(",",":"),default=str)
            digest=hashlib.sha256(payload.encode()).hexdigest()
            Audit.objects.filter(pk=event.pk).update(previous_hash=previous,event_hash=digest)
            previous=digest

class Migration(migrations.Migration):
    dependencies=[("core","0015_auditredaction_auditevent_event_hash_and_more")]
    operations=[migrations.RunPython(forward,migrations.RunPython.noop)]
