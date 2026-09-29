from django.contrib import admin
from .models import (
    AuditEvent, Branch, Client, Credential, CredentialType, Membership, Organization,
    DocumentType, Notification, OrganizationDomain, Person, PersonDocument, Punch, Shift,
    Site, TimePolicy, TrainingRecord,
)

@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin): list_display = ("display_name", "slug", "timezone", "updated_at")
@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin): list_display = ("user", "organization", "role", "active")
@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin): list_display = ("name", "organization", "city", "active")
@admin.register(Person)
class PersonAdmin(admin.ModelAdmin): list_display = ("full_name", "organization", "branch", "status")
@admin.register(OrganizationDomain)
class OrganizationDomainAdmin(admin.ModelAdmin): list_display = ("hostname", "organization", "verified")
@admin.register(Client)
class ClientAdmin(admin.ModelAdmin): list_display = ("name", "organization", "active")
@admin.register(Site)
class SiteAdmin(admin.ModelAdmin): list_display = ("name", "client", "organization", "active")
@admin.register(CredentialType)
class CredentialTypeAdmin(admin.ModelAdmin): list_display = ("name", "organization", "blocks_scheduling", "blocks_clock_in")
@admin.register(Credential)
class CredentialAdmin(admin.ModelAdmin): list_display = ("person", "credential_type", "status", "expires_on")
@admin.register(TrainingRecord)
class TrainingRecordAdmin(admin.ModelAdmin): list_display = ("person", "course_name", "completed_on", "expires_on")
@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin): list_display = ("site", "officer", "starts_at", "ends_at", "status")
@admin.register(Punch)
class PunchAdmin(admin.ModelAdmin): list_display = ("person", "kind", "occurred_at", "review_status", "offline")
@admin.register(TimePolicy)
class TimePolicyAdmin(admin.ModelAdmin): list_display = ("organization", "rounding_mode", "rounding_minutes", "overtime_after_hours")
@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin): list_display = ("name", "organization", "retention_days", "active")
@admin.register(PersonDocument)
class PersonDocumentAdmin(admin.ModelAdmin): list_display = ("original_name", "person", "document_type", "scan_status", "created_at")
@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin): list_display = ("subject", "recipient", "channel", "status", "created_at")
@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "organization", "action", "actor", "target_type")
    readonly_fields = ("id", "organization", "actor", "action", "target_type", "target_id", "metadata", "occurred_at")
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False
