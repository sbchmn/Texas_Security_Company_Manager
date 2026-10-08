from datetime import datetime, time, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_http_methods

from .auth import membership_required
from .forms import PunchAdjustmentForm
from .models import AuditEvent, HoldOver, Punch, PunchAdjustment
from .scope import for_membership
from .services import haversine_meters, lock_subject, payroll_lock_state, queue_notice, schedule_week_start, week_offset_for
from .timekeeping import break_conflicts, effective_time, invalidate_drafts, pair_tours


def timesheet_window(request, run=None):
    from .views import WEEK_WINDOW
    if run is not None:
        return {"sheet_start": timezone.localdate(run.period_start),
                "sheet_end": timezone.localdate(run.period_end), "sheet_offset": 0,
                "sheet_custom": False, "sheet_current": False, "sheet_previous": None, "sheet_next": None}
    try:
        offset = max(-WEEK_WINDOW, min(WEEK_WINDOW, int(request.GET.get("week", "0"))))
    except ValueError:
        raise Http404("Choose a valid timesheet week.")
    week_start = schedule_week_start(offset=offset, organization=request.organization)
    try:
        start = datetime.strptime(request.GET.get("start") or week_start.isoformat(), "%Y-%m-%d").date()
        end = datetime.strptime(request.GET.get("end") or (week_start + timedelta(days=6)).isoformat(), "%Y-%m-%d").date()
    except ValueError:
        raise Http404("Choose valid timesheet dates.")
    if end < start or (end - start).days > 92:
        raise Http404("Choose a timesheet range of at most 93 days.")
    custom = start != week_start or end != week_start + timedelta(days=6)
    if custom:
        offset = week_offset_for(start, organization=request.organization)
    return {"sheet_start": start, "sheet_end": end, "sheet_offset": offset,
            "sheet_custom": custom, "sheet_current": offset == 0 and not custom,
            "sheet_previous": offset - 1 if -WEEK_WINDOW < offset <= WEEK_WINDOW else None,
            "sheet_next": offset + 1 if -WEEK_WINDOW <= offset < WEEK_WINDOW else None}


def filtered_punches(request, queryset, run=None):
    try:
        if request.GET.get("person"):
            person = get_object_or_404(request.organization.people, pk=UUID(request.GET["person"]))
            queryset = queryset.filter(person=person)
        if request.GET.get("site"):
            site = get_object_or_404(request.organization.sites, pk=UUID(request.GET["site"]))
            queryset = queryset.filter(shift__site=site)
    except ValueError:
        raise Http404("Choose a valid officer or site.")
    if run:
        return queryset.filter(occurred_at__gte=run.period_start, occurred_at__lt=run.period_end)
    window = timesheet_window(request)
    start, end = window["sheet_start"], window["sheet_end"]
    return queryset.filter(
        occurred_at__gte=timezone.make_aware(datetime.combine(start, time.min)),
        occurred_at__lt=timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min)),
    )


def timesheet_context(request, queryset, run=None):
    from django.core.paginator import Paginator
    # Pair complete evidence before paginating, so neither a page nor the review filter splits a tour.
    scope = for_membership(request.membership)
    rows = list(queryset.select_related("person", "shift__site__client").prefetch_related("adjustments"))
    matched_ids = {row.pk for row in rows}
    if rows:
        window = timesheet_window(request, run)
        context_start = timezone.make_aware(datetime.combine(window["sheet_start"] - timedelta(days=1), time.min))
        context_end = timezone.make_aware(datetime.combine(window["sheet_end"] + timedelta(days=2), time.min))
        surrounding = Q(shift_id__in={row.shift_id for row in rows if row.shift_id is not None}) | Q(
            shift__isnull=True, occurred_at__gte=context_start, occurred_at__lt=context_end,
        )
        rows = list(scope.filter_punches(request.organization.punches.all()).filter(
            surrounding, person_id__in={row.person_id for row in rows},
        ).select_related("person", "shift__site__client").prefetch_related("adjustments"))
    tours = pair_tours(rows)
    tours = [tour for tour in tours if any(row.pk in matched_ids for row in tour["events"])]
    conflicts = break_conflicts(request.organization, rows)
    for tour in tours:
        if tour["start"].shift_id in conflicts:
            tour["issues"].append("Manual break designation conflicts with recorded break punches.")
            tour["needs_review"] = True
        tour["hours"] = tour["paid_minutes"] / 60 if tour["paid_minutes"] is not None else None
    if request.GET.get("view") == "review":
        tours = [tour for tour in tours if tour["needs_review"]]
    page = Paginator(tours, 50).get_page(request.GET.get("sheet_page"))
    return {**timesheet_window(request, run), "tours": page.object_list, "sheet_page": page,
            "sheet_people": scope.filter_people(request.organization.people.all()).order_by("last_name", "first_name"),
            "sheet_sites": scope.filter_sites(request.organization.sites.all()).select_related("client"),
            "sheet_advanced": any(request.GET.get(name) for name in ("person", "site", "start", "end")) or request.GET.get("view") == "review",
            "sheet_view": request.GET.get("view", "all")}


def legacy_review(request):
    destination = reverse("time_review")
    if request.GET:
        destination += "?" + request.GET.urlencode()
    return redirect(destination)


def _reviewers():
    from .views import TIME_REVIEWERS
    return TIME_REVIEWERS


@never_cache
@xframe_options_sameorigin
@require_http_methods(["GET", "POST"])
@membership_required(*_reviewers())
@transaction.atomic
def punch_detail(request, punch_id):
    from .views import _record_open
    scope = for_membership(request.membership)
    punch = get_object_or_404(scope.filter_punches(request.organization.punches.all()).select_related(
        "person", "shift__site__client", "selfie__document_type", "checkpoint",
    ).prefetch_related("adjustments"), pk=punch_id)
    current_time = effective_time(punch)
    form = PunchAdjustmentForm(request.POST if request.method == "POST" and request.POST.get("action") == "correct" else None,
                               initial={"proposed_at": timezone.localtime(current_time), "reason": ""})
    if request.method == "POST":
        # Serialize competing corrections/classifications to this punch.
        punch = Punch.objects.select_for_update().get(pk=punch.pk)
        current_time = effective_time(punch)
        try:
            state = payroll_lock_state(request.organization, punch.occurred_at, *lock_subject(punch))
            if state["locked"]:
                raise ValidationError(f"This payroll period is locked — {state['by']}.")
            if payroll_lock_state(request.organization, current_time, *lock_subject(punch))["locked"]:
                raise ValidationError("The effective punch time falls in a locked payroll period.")
            action = request.POST.get("action")
            if action == "correct":
                if not form.is_valid():
                    raise ValidationError("Correct the time and provide a reason of at least five characters.")
                proposed = form.cleaned_data["proposed_at"]
                if payroll_lock_state(request.organization, proposed, *lock_subject(punch))["locked"]:
                    raise ValidationError("The corrected time falls in a locked payroll period.")
                if proposed > timezone.now() + timedelta(minutes=5):
                    raise ValidationError("A correction cannot record future work.")
                adjustment = PunchAdjustment.objects.create(
                    organization=request.organization, punch=punch, requested_by=request.user,
                    proposed_at=proposed, reason=form.cleaned_data["reason"],
                    status=PunchAdjustment.Status.APPROVED, reviewed_by=request.user, reviewed_at=timezone.now(),
                    review_note="Manager correction from Timesheets.",
                )
                AuditEvent.objects.create(organization=request.organization, actor=request.user,
                    action="punch.corrected", target_type="punch", target_id=str(punch.pk),
                    metadata={"before": current_time.isoformat(), "after": proposed.isoformat(),
                              "reason": form.cleaned_data["reason"]})
                employee_user = punch.person.user
                kind_label = Punch.Kind(punch.kind).label.lower()
                if employee_user is not None and employee_user.pk != request.user.pk:
                    queue_notice(organization=request.organization, recipients={employee_user.pk},
                        event_type="punch.correction_approved",
                        subject="Your punch time was corrected",
                        body=f"A reviewer corrected your {kind_label} from {timezone.localtime(current_time)} to {timezone.localtime(proposed)}. Reason: {form.cleaned_data['reason']}",
                        dedup_key=f"punch.correction:{adjustment.pk}:approved",
                        sms={"kind": kind_label, "at": proposed, "note": form.cleaned_data["reason"]})
            elif action == "break_pay":
                if punch.kind != Punch.Kind.BREAK_START:
                    raise ValidationError("Classify pay on the break-start punch.")
                if punch.shift is not None:
                    related = request.organization.punches.filter(person=punch.person, shift=punch.shift).prefetch_related("adjustments")
                    for tour in pair_tours(related):
                        if any(row.pk == punch.pk for row in tour["events"]):
                            if any(payroll_lock_state(request.organization, effective_time(row), *lock_subject(punch))["locked"]
                                   for row in tour["events"]):
                                raise ValidationError("This break belongs to a tour in a locked payroll period.")
                reason = request.POST.get("reason", "").strip()
                if len(reason) < 5 or len(reason) > 255 or request.POST.get("paid") not in ("yes", "no"):
                    raise ValidationError("Choose paid or unpaid and give a reason of 5–255 characters.")
                before = punch.break_paid
                punch.break_paid = request.POST["paid"] == "yes"
                punch.break_pay_reason = reason
                punch.save(update_fields=["break_paid", "break_pay_reason"])
                AuditEvent.objects.create(organization=request.organization, actor=request.user,
                    action="break.classified", target_type="punch", target_id=str(punch.pk),
                    metadata={"before_paid": before, "paid": punch.break_paid, "reason": reason})
            else:
                raise ValidationError("Choose a valid timesheet action.")
            invalidate_drafts(punch)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, "Saved. Original punch evidence is unchanged. Regenerate any existing payroll draft.")
            return redirect(reverse("punch_detail", args=[punch.pk]) + ("?modal=1" if request.GET.get("modal") else ""))
    recorded = request.organization.audit_events.filter(action="punch.recorded", target_type="punch", target_id=str(punch.pk)).first()
    evidence = recorded.metadata.get("evidence", {}) if recorded else {}
    location = evidence.get("location", {})
    shift = punch.shift
    site = shift.site if shift is not None else None
    if site is None and punch.source == "kiosk" and punch.device_id:
        kiosk = request.organization.clock_kiosks.select_related("site__client").filter(pk=punch.device_id).first()
        site = kiosk.site if kiosk else None
    distance = None
    if site and all(value is not None for value in (punch.latitude, punch.longitude, site.latitude, site.longitude)):
        distance = round(haversine_meters(punch.latitude, punch.longitude, site.latitude, site.longitude))
    map_data = None
    if punch.latitude is not None and punch.longitude is not None:
        map_data = {"provider": settings.TIMESHEET_MAP_PROVIDER, "key": settings.GOOGLE_MAPS_BROWSER_KEY,
                    "tile_url": settings.TIMESHEET_MAP_TILE_URL, "attribution": settings.TIMESHEET_MAP_ATTRIBUTION,
                    "lat": float(punch.latitude), "lng": float(punch.longitude),
                    "site_lat": float(site.latitude) if site and site.latitude is not None else None,
                    "site_lng": float(site.longitude) if site and site.longitude is not None else None,
                    "radius": site.geofence_radius_meters if site else None}
    related = request.organization.punches.filter(person=punch.person, shift=shift).select_related(
        "person", "shift__site").prefetch_related("adjustments")
    if shift is None:
        related = related.filter(occurred_at__date=timezone.localdate(punch.occurred_at))
    tours = pair_tours(related)
    history = request.organization.audit_events.filter(
        target_type__in=("punch", "punch_adjustment"),
        target_id__in=[str(punch.pk), *[str(item.pk) for item in PunchAdjustment.objects.filter(punch=punch)]],
    ).exclude(action="punch.recorded").select_related("actor").order_by("-occurred_at")
    detail_locked = any(payroll_lock_state(request.organization, at, *lock_subject(punch))["locked"]
                        for at in (punch.occurred_at, current_time))
    response = render(request, "core/punch_detail.html", {
        "punch": punch, "effective_at": current_time, "correction_form": form, "location": location,
        "kiosk_name": evidence.get("kiosk_name"), "site": site, "map_data": map_data,
        "distance": distance, "recorded_policy": recorded.metadata if recorded else None,
        "selfie_visible": punch.selfie is not None and _record_open(request, punch.selfie),
        "tours": [tour for tour in tours if any(row.pk == punch.pk for row in tour["events"])],
        "holdovers": HoldOver.objects.filter(organization=request.organization, shift=shift).filter(
            Q(officer=punch.person) | Q(officer__isnull=True)).select_related("relief", "recorded_by") if shift is not None else [],
        "history": history,
        "detail_locked": detail_locked,
        "detail_base": "core/punch_modal_base.html" if request.GET.get("modal") else "base.html",
    })
    if map_data and settings.TIMESHEET_MAP_PROVIDER == "osm":
        origin = urlsplit(settings.TIMESHEET_MAP_TILE_URL)
        response["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'self'; "
            f"form-action 'self'; img-src 'self' data: {origin.scheme}://{origin.netloc}; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'"
        )
    elif map_data and settings.TIMESHEET_MAP_PROVIDER == "google":
        response["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'self'; form-action 'self'; "
            "img-src 'self' data: https://*.googleapis.com https://*.gstatic.com https://*.google.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self' https://maps.googleapis.com https://maps.gstatic.com; "
            "connect-src 'self' https://*.googleapis.com https://*.gstatic.com"
        )
    else:
        response["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'self'; "
            "form-action 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'"
        )
    return response
