from django.db import migrations


GUARDS = {
    "signingrequest": (
        "(SELECT COUNT(*) FROM core_onboardingtask t WHERE t.id=NEW.task_id AND t.organization_id=NEW.organization_id)=0 "
        "OR (SELECT COUNT(*) FROM core_documenttype d WHERE d.id=NEW.document_type_id AND d.organization_id=NEW.organization_id)=0"
    ),
    "signedartifact": (
        "(SELECT COUNT(*) FROM core_signingrequest r "
        "JOIN core_onboardingtask t ON t.id=r.task_id "
        "JOIN core_persondocument d ON d.id=NEW.document_id "
        "WHERE r.id=NEW.request_id AND d.organization_id=r.organization_id "
        "AND d.person_id=t.person_id AND d.document_type_id=r.document_type_id)=0"
    ),
}


def install(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table, condition in GUARDS.items():
            for action in ("INSERT", "UPDATE"):
                name = f"core_{table}_tenant_{action.lower()}"
                cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
                cursor.execute(
                    f"CREATE TRIGGER {name} BEFORE {action} ON core_{table} FOR EACH ROW BEGIN "
                    f"IF {condition} THEN SIGNAL SQLSTATE '45000' "
                    "SET MESSAGE_TEXT='cross-tenant signing reference'; END IF; END"
                )


def remove(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table in GUARDS:
            for action in ("insert", "update"):
                cursor.execute(f"DROP TRIGGER IF EXISTS core_{table}_tenant_{action}")


class Migration(migrations.Migration):
    dependencies = [("core", "0058_document_signing")]
    operations = [migrations.RunPython(install, remove)]
