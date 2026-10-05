from django.db import migrations

# The last tenant-linked table without a cross-tenant reference guard. `core_dispositionrequest` FKs a
# `PersonDocument`, and every other table that reaches across to a tenant-owned row was policed by 0012
# / 0018 / 0025 / 0055 — this one was simply never in the list.
#
# The application cannot create the bad state: `views.disposition_request` resolves the document
# through `request.organization`'s own queryset, so a crafted id is a 404. That is why this landed as a
# disclosed gap rather than an emergency — and it is also exactly why the guard is worth installing:
# the *only* thing standing between a raw `UPDATE` and "one company's destruction order pointing at
# another company's file" is application code, and this schema's whole posture is that a decision this
# consequential does not rest on application code alone. A disposition row is the paper trail for
# deleting somebody's record.
#
# `document_id` is NOT NULL (`on_delete=PROTECT`), so unlike the nullable references in 0045 and 0055
# there is no `IS NOT NULL` prefix to get wrong here, and no `COUNT()`-style clause to precede. The
# comparison is the direct subselect 0025 uses for `document_type_id`.
TRIGGERS = {
    "core_disposition_tenant_insert": (
        "CREATE TRIGGER core_disposition_tenant_insert BEFORE INSERT ON core_dispositionrequest "
        "FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_persondocument WHERE id=NEW.document_id) "
        "<> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant disposition reference'; "
        "END IF; END"
    ),
    "core_disposition_tenant_update": (
        "CREATE TRIGGER core_disposition_tenant_update BEFORE UPDATE ON core_dispositionrequest "
        "FOR EACH ROW BEGIN IF (SELECT organization_id FROM core_persondocument WHERE id=NEW.document_id) "
        "<> NEW.organization_id THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='cross-tenant disposition reference'; "
        "END IF; END"
    ),
}


def install(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    with schema_editor.connection.cursor() as cursor:
        for name, sql in TRIGGERS.items():
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
            cursor.execute(sql)


def remove(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    with schema_editor.connection.cursor() as cursor:
        for name in TRIGGERS:
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0055_clock_selfie_capture"),
    ]

    operations = [
        migrations.RunPython(install, remove),
    ]
