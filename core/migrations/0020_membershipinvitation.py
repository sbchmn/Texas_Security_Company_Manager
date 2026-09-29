import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("core", "0019_extended_tenant_guards"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name="MembershipInvitation", fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("email", models.EmailField(max_length=254)),
            ("role", models.CharField(choices=[("owner", "Owner"), ("admin", "Administrator")], max_length=30)),
            ("token_hash", models.CharField(max_length=64, unique=True)),
            ("expires_at", models.DateTimeField()), ("accepted_at", models.DateTimeField(blank=True, null=True)), ("created_at", models.DateTimeField(auto_now_add=True)),
            ("invited_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sent_membership_invitations", to=settings.AUTH_USER_MODEL)),
            ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="membership_invitations", to="core.organization")),
        ], options={"ordering": ["-created_at"]}),
        migrations.AddConstraint(model_name="membershipinvitation", constraint=models.UniqueConstraint(condition=models.Q(("accepted_at__isnull", True)), fields=("organization", "email"), name="one_pending_invitation_per_org_email")),
    ]
