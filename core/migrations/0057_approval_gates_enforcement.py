from django.db import migrations, models


class Migration(migrations.Migration):
    """Approval gates enforcement (owner ruling of 2026-10-05), without weakening a live install.

    The ruling is that a requirement nobody has approved must not be the reason a guard cannot clock in
    or cannot be assigned: an unapproved row has no source, no reference and no named approver, so the
    firm cannot say where the refusal came from.

    Applying that to rows that already exist is the part that had to be decided deliberately. Turning the
    gate on with no escape would stop every unapproved requirement from enforcing — silently, on upgrade
    day, in installations that set `blocks_clock_in` before the approval columns meant anything and are
    relying on that refusal today. That is a change of behaviour dressed as a bug fix, so this migration
    adds a flag whose **default is True**: existing rows keep doing exactly what they did, and the flag
    says out loud, on the compliance matrix, that they are enforcing without approval.

    The default is safe in the direction that matters, and it is bounded: `credential_type_create`
    clears the flag for every row made from the settings screen, and the edit path clears it when a row
    is approved. So no requirement created from now on can gate anything until somebody approves it,
    while the legacy set is a visible, closeable list rather than a silent one.

    Only `CredentialType` gets the column. `ComplianceRule` has no enforcement flags at all — its
    obligation already stops at `unevaluated_reason` ("not approved, so not enforced yet"), which is why
    the drafted Texas duties could be seeded safely in the first place.
    """

    dependencies = [
        ("core", "0056_disposition_tenant_guard"),
    ]

    operations = [
        migrations.AddField(
            model_name="credentialtype",
            name="enforcement_grandfathered",
            field=models.BooleanField(
                default=True,
                help_text="Legacy escape for rows that were already refusing assignments and clock-ins "
                          "before approval became a precondition for enforcement. The settings screen "
                          "clears it on every row it creates or edits, so a requirement made from now on "
                          "enforces only once a named person has approved it; an existing row keeps "
                          "behaving as its installation chose until somebody approves it or turns the "
                          "flags off. See the matrix's \"Grandfathered\" label."),
        ),
    ]
