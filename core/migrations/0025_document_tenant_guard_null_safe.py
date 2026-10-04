from django.db import migrations

# core_persondocument.person_id became nullable so a company record (handbook, licence) can
# exist without a personnel file. The original guard compared
# (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id,
# which yields NULL — not FALSE — for a NULL person, so the cross-tenant check was passing
# only through NULL propagation. This restates it with the explicit IS NOT NULL guard that
# every other nullable reference in 0012 already uses.
TRIGGERS={
"core_document_tenant_insert": "CREATE TRIGGER core_document_tenant_insert BEFORE INSERT ON core_persondocument FOR EACH ROW BEGIN IF (NEW.person_id IS NOT NULL AND (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id) OR (SELECT organization_id FROM core_documenttype WHERE id=NEW.document_type_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant document reference'; END IF; END",
"core_document_tenant_update": "CREATE TRIGGER core_document_tenant_update BEFORE UPDATE ON core_persondocument FOR EACH ROW BEGIN IF (NEW.person_id IS NOT NULL AND (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id) OR (SELECT organization_id FROM core_documenttype WHERE id=NEW.document_type_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant document reference'; END IF; END",
}

def create(apps,schema_editor):
    if schema_editor.connection.vendor!="mysql":return
    with schema_editor.connection.cursor() as cursor:
        for name,sql in TRIGGERS.items():cursor.execute(f"DROP TRIGGER IF EXISTS {name}");cursor.execute(sql)

def drop(apps,schema_editor):
    if schema_editor.connection.vendor!="mysql":return
    original={
    "core_document_tenant_insert": "CREATE TRIGGER core_document_tenant_insert BEFORE INSERT ON core_persondocument FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id OR (SELECT organization_id FROM core_documenttype WHERE id=NEW.document_type_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant document reference'; END IF; END",
    "core_document_tenant_update": "CREATE TRIGGER core_document_tenant_update BEFORE UPDATE ON core_persondocument FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id OR (SELECT organization_id FROM core_documenttype WHERE id=NEW.document_type_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant document reference'; END IF; END",
    }
    with schema_editor.connection.cursor() as cursor:
        for name,sql in original.items():cursor.execute(f"DROP TRIGGER IF EXISTS {name}");cursor.execute(sql)

class Migration(migrations.Migration):
    dependencies=[("core","0024_people_centric_records")]
    operations=[migrations.RunPython(create,drop)]
