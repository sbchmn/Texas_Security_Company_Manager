from django.db import migrations

TRIGGERS = {
"core_site_tenant_insert": "CREATE TRIGGER core_site_tenant_insert BEFORE INSERT ON core_site FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_client WHERE id=NEW.client_id) <> NEW.organization_id OR (NEW.branch_id IS NOT NULL AND (SELECT organization_id FROM core_branch WHERE id=NEW.branch_id) <> NEW.organization_id) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant site reference'; END IF; END",
"core_site_tenant_update": "CREATE TRIGGER core_site_tenant_update BEFORE UPDATE ON core_site FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_client WHERE id=NEW.client_id) <> NEW.organization_id OR (NEW.branch_id IS NOT NULL AND (SELECT organization_id FROM core_branch WHERE id=NEW.branch_id) <> NEW.organization_id) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant site reference'; END IF; END",
"core_credential_tenant_insert": "CREATE TRIGGER core_credential_tenant_insert BEFORE INSERT ON core_credential FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id OR (SELECT organization_id FROM core_credentialtype WHERE id=NEW.credential_type_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant credential reference'; END IF; END",
"core_credential_tenant_update": "CREATE TRIGGER core_credential_tenant_update BEFORE UPDATE ON core_credential FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id OR (SELECT organization_id FROM core_credentialtype WHERE id=NEW.credential_type_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant credential reference'; END IF; END",
"core_shift_tenant_insert": "CREATE TRIGGER core_shift_tenant_insert BEFORE INSERT ON core_shift FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_site WHERE id=NEW.site_id) <> NEW.organization_id OR (NEW.officer_id IS NOT NULL AND (SELECT organization_id FROM core_person WHERE id=NEW.officer_id) <> NEW.organization_id) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant shift reference'; END IF; END",
"core_shift_tenant_update": "CREATE TRIGGER core_shift_tenant_update BEFORE UPDATE ON core_shift FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_site WHERE id=NEW.site_id) <> NEW.organization_id OR (NEW.officer_id IS NOT NULL AND (SELECT organization_id FROM core_person WHERE id=NEW.officer_id) <> NEW.organization_id) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant shift reference'; END IF; END",
"core_punch_tenant_insert": "CREATE TRIGGER core_punch_tenant_insert BEFORE INSERT ON core_punch FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id OR (NEW.shift_id IS NOT NULL AND (SELECT organization_id FROM core_shift WHERE id=NEW.shift_id) <> NEW.organization_id) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant punch reference'; END IF; END",
"core_punch_tenant_update": "CREATE TRIGGER core_punch_tenant_update BEFORE UPDATE ON core_punch FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id OR (NEW.shift_id IS NOT NULL AND (SELECT organization_id FROM core_shift WHERE id=NEW.shift_id) <> NEW.organization_id) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant punch reference'; END IF; END",
"core_document_tenant_insert": "CREATE TRIGGER core_document_tenant_insert BEFORE INSERT ON core_persondocument FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id OR (SELECT organization_id FROM core_documenttype WHERE id=NEW.document_type_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant document reference'; END IF; END",
"core_document_tenant_update": "CREATE TRIGGER core_document_tenant_update BEFORE UPDATE ON core_persondocument FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_person WHERE id=NEW.person_id) <> NEW.organization_id OR (SELECT organization_id FROM core_documenttype WHERE id=NEW.document_type_id) <> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant document reference'; END IF; END",
}

def create_guards(apps,schema_editor):
    if schema_editor.connection.vendor != "mysql": return
    with schema_editor.connection.cursor() as cursor:
        for name,sql in TRIGGERS.items():
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
            cursor.execute(sql)

def drop_guards(apps,schema_editor):
    if schema_editor.connection.vendor != "mysql": return
    with schema_editor.connection.cursor() as cursor:
        for name in TRIGGERS: cursor.execute(f"DROP TRIGGER IF EXISTS {name}")

class Migration(migrations.Migration):
    dependencies=[("core","0011_organizationdomain_status_and_more")]
    operations=[migrations.RunPython(create_guards,drop_guards)]
