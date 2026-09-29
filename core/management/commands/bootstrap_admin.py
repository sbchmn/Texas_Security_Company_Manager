import os
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from core.models import AuditEvent, Branch, Membership, Organization

class Command(BaseCommand):
    help = "Create the first organization owner from BOOTSTRAP_* environment variables."
    @transaction.atomic
    def handle(self, *args, **options):
        email=os.getenv("BOOTSTRAP_EMAIL", "").strip(); password=os.getenv("BOOTSTRAP_PASSWORD", "")
        name=os.getenv("BOOTSTRAP_COMPANY", "Lone Star Security").strip(); slug=os.getenv("BOOTSTRAP_SLUG", "lone-star-security").strip()
        if not email or not password: raise CommandError("BOOTSTRAP_EMAIL and BOOTSTRAP_PASSWORD are required")
        User=get_user_model(); user, created=User.objects.get_or_create(username=email, defaults={"email": email, "is_staff": True})
        if created: user.set_password(password); user.save()
        org, _=Organization.objects.get_or_create(slug=slug, defaults={"legal_name": name, "display_name": name})
        Membership.objects.update_or_create(organization=org, user=user, defaults={"role": Membership.Role.OWNER, "active": True})
        Branch.objects.get_or_create(organization=org, name="Main Branch")
        AuditEvent.objects.get_or_create(organization=org, action="organization.bootstrapped", target_type="organization", target_id=str(org.pk), defaults={"actor": user})
        self.stdout.write(self.style.SUCCESS(f"Owner {email} is ready for {org.display_name}"))
