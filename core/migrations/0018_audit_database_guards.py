from django.db import migrations

TRIGGERS={
"core_audit_no_update":"CREATE TRIGGER core_audit_no_update BEFORE UPDATE ON core_auditevent FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='audit events are immutable'",
"core_audit_no_delete":"CREATE TRIGGER core_audit_no_delete BEFORE DELETE ON core_auditevent FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='audit events are immutable'",
}
def create(apps,schema_editor):
    if schema_editor.connection.vendor!="mysql":return
    with schema_editor.connection.cursor() as cursor:
        for name,sql in TRIGGERS.items():cursor.execute(f"DROP TRIGGER IF EXISTS {name}");cursor.execute(sql)
def drop(apps,schema_editor):
    if schema_editor.connection.vendor!="mysql":return
    with schema_editor.connection.cursor() as cursor:
        for name in TRIGGERS:cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
class Migration(migrations.Migration):
    dependencies=[("core","0017_punch_device_id_punch_device_sequence_checkpoint_and_more")]
    operations=[migrations.RunPython(create,drop)]
