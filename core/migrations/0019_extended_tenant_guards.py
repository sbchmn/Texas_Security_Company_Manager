from django.db import migrations

TRIGGERS={
"core_customvalue_tenant_insert":"CREATE TRIGGER core_customvalue_tenant_insert BEFORE INSERT ON core_personcustomvalue FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id OR (SELECT organization_id FROM core_customfielddefinition WHERE id=NEW.definition_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant custom value'; END IF; END",
"core_customvalue_tenant_update":"CREATE TRIGGER core_customvalue_tenant_update BEFORE UPDATE ON core_personcustomvalue FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id OR (SELECT organization_id FROM core_customfielddefinition WHERE id=NEW.definition_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant custom value'; END IF; END",
"core_checkpoint_tenant_insert":"CREATE TRIGGER core_checkpoint_tenant_insert BEFORE INSERT ON core_checkpoint FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_site WHERE id=NEW.site_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant checkpoint'; END IF; END",
"core_checkpoint_tenant_update":"CREATE TRIGGER core_checkpoint_tenant_update BEFORE UPDATE ON core_checkpoint FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_site WHERE id=NEW.site_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant checkpoint'; END IF; END",
"core_clockdevice_tenant_insert":"CREATE TRIGGER core_clockdevice_tenant_insert BEFORE INSERT ON core_offlineclockdevice FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant clock device'; END IF; END",
"core_clockdevice_tenant_update":"CREATE TRIGGER core_clockdevice_tenant_update BEFORE UPDATE ON core_offlineclockdevice FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant clock device'; END IF; END",
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
    dependencies=[("core","0018_audit_database_guards")]
    operations=[migrations.RunPython(create,drop)]
