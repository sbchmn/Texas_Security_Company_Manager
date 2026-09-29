from django.urls import path
from . import views
urlpatterns = [
    path("healthz", views.health, name="health"), path("readyz",views.ready,name="ready"), path("theme.css",views.theme_css,name="theme_css"), path("manifest.webmanifest", views.manifest, name="manifest"),
    path("service-worker.js", views.service_worker, name="service_worker"),
    path("", views.dashboard, name="dashboard"), path("people/", views.people, name="people"),
    path("people/new/", views.person_create, name="person_create"), path("branches/new/", views.branch_create, name="branch_create"),
    path("settings/branding/", views.branding, name="branding"),
    path("locations/", views.locations, name="locations"), path("locations/clients/new/", views.client_create, name="client_create"), path("locations/sites/new/", views.site_create, name="site_create"),
    path("compliance/", views.compliance, name="compliance"), path("compliance/types/new/", views.credential_type_create, name="credential_type_create"), path("compliance/credentials/new/", views.credential_create, name="credential_create"),
    path("schedule/", views.schedule, name="schedule"), path("schedule/new/", views.shift_create, name="shift_create"),
    path("clock/", views.clock, name="clock"), path("api/punches/", views.punch_api, name="punch_api"),
    path("settings/time/", views.time_policy, name="time_policy"), path("payroll/export/", views.payroll_export, name="payroll_export"),
    path("documents/",views.documents,name="documents"),path("documents/types/new/",views.document_type_create,name="document_type_create"),path("documents/upload/",views.document_upload,name="document_upload"),path("documents/<uuid:document_id>/download/",views.document_download,name="document_download"),
    path("documents/retention/",views.retention_review,name="retention_review"),path("documents/<uuid:document_id>/disposition/",views.disposition_request,name="disposition_request"),path("documents/<uuid:document_id>/legal-hold/",views.legal_hold_toggle,name="legal_hold_toggle"),path("documents/dispositions/<uuid:request_id>/execute/",views.disposition_execute,name="disposition_execute"),
    path("training/",views.training,name="training"),path("training/new/",views.training_create,name="training_create"),
    path("imports/",views.imports,name="imports"),path("imports/<uuid:batch_id>/apply/",views.import_apply,name="import_apply"),
    path("notifications/",views.notifications,name="notifications"),path("notifications/<uuid:notification_id>/read/",views.notification_read,name="notification_read"),
    path("time/review/",views.time_review,name="time_review"),path("time/punches/<uuid:punch_id>/review/",views.punch_review,name="punch_review"),path("time/punches/<uuid:punch_id>/correction/",views.adjustment_request,name="adjustment_request"),path("time/adjustments/<uuid:adjustment_id>/review/",views.adjustment_review,name="adjustment_review"),
    path("payroll/",views.payroll,name="payroll"),path("payroll/<uuid:run_id>/approve/",views.payroll_approve,name="payroll_approve"),path("payroll/<uuid:run_id>/export/",views.payroll_run_export,name="payroll_run_export"),
    path("settings/security/",views.security_settings,name="security_settings"),
]
