"""Bounded manager authority, resolved once per request.

``docs/discovery-decisions.md`` requires branch managers and field supervisors to review and
approve "within their assigned scope", and the same document lists organization, branch,
client, and site as the authorization axes. A role on its own is organization-wide, so this
module turns the ``AuthorityScope`` rows a membership holds into the filters that every list
view, per-object route, and approval decision passes through.

Each grant expands to the records it actually covers:

* **branch** — the personnel filed under it, and every post stood at a site belonging to it;
* **contract** — every post at that client's sites, and the officers who stand them;
* **site** — that post, and every officer assigned to it.

Contract and site grants reach people through the assignment rather than the personnel file,
because a guard on someone else's payroll still has to be supervised where they stand.

Resolution costs three queries for a bounded user (grants, sites, people), one for a field role
that was never given a grant, and none for a company-level role; it is memoised on the request.
The id sets are deliberately materialised: they are bounded by the tenant roster, and keeping
them as concrete sets lets a view filter, count, and object-test against the same answer without
re-deriving it.
"""

from collections import defaultdict

from django.db.models import Q

from .models import AuthorityScope, Membership, Person, Site

# Only the two field roles carry bounded authority. Owner, administrator, HR / Compliance,
# payroll approver, and read-only auditor are company-level functions and are never narrowed,
# so an administrator granted one branch keeps the whole company.
SCOPE_CAPABLE_ROLES = (Membership.Role.SCHEDULER, Membership.Role.SUPERVISOR)

# Roles that decide a coverage gap wherever it is, so a scoped manager is added to them
# rather than replacing them.
COMPANY_MANAGING_ROLES = (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR)


def _pk(value):
    return value.pk if hasattr(value, "pk") else value


class ActorScope:
    """What one membership may list, open, and decide."""

    def __init__(self, membership, rows=()):
        self.membership = membership
        self.rows = list(rows)
        self.restricted = bool(self.rows)
        self.branch_ids = frozenset(row.branch_id for row in self.rows if row.branch_id)
        # AUTH-1. A grant that follows the member's own branch is resolved here, at request time, so
        # the reach is whatever the personnel file says today — which is the entire difference from a
        # frozen branch id. With no branch on the file the grant resolves to nothing rather than to
        # everything: an unanswered question is not a licence, and the authority screen says so.
        self.inherited_branch_id = next((row.person_branch_id for row in self.rows if row.follows_own_branch), None)
        if self.inherited_branch_id:
            self.branch_ids = self.branch_ids | {self.inherited_branch_id}
        self.client_ids = frozenset(row.client_id for row in self.rows if row.client_id)
        explicit_sites = frozenset(row.site_id for row in self.rows if row.site_id)
        organization_id = membership.organization_id if membership else None
        derived_sites = set()
        if self.branch_ids or self.client_ids:
            # Tenant filter first: a scope row pointing at another organization's site would
            # otherwise widen this user's authority instead of narrowing it.
            derived_sites = set(Site.objects.filter(organization_id=organization_id).filter(
                Q(branch_id__in=self.branch_ids) | Q(client_id__in=self.client_ids)
            ).values_list("id", flat=True))
        self.site_ids = frozenset(explicit_sites | derived_sites)
        if self.restricted:
            self.person_ids = frozenset(Person.objects.filter(organization_id=organization_id).filter(
                Q(branch_id__in=self.branch_ids) | Q(shifts__site_id__in=self.site_ids)
            ).values_list("id", flat=True))
        else:
            self.person_ids = None

    @property
    def summary(self):
        """Short human description for the sidebar and the authority screen."""
        if not self.restricted:
            return "Whole company"
        labels = [row.label for row in self.rows]
        if len(labels) > 3:
            return f"{len(labels)} scopes: " + ", ".join(labels[:2]) + "…"
        return "; ".join(labels)

    # -- object-level tests: the per-object routes answer 404, not "filtered out of a list" --

    def permits_person(self, person):
        return (not self.restricted) or _pk(person) in self.person_ids

    def permits_site(self, site):
        return (not self.restricted) or _pk(site) in self.site_ids

    def permits_shift(self, shift):
        if not self.restricted:
            return True
        return shift.site_id in self.site_ids or (shift.officer_id and shift.officer_id in self.person_ids)

    def permits_punch(self, punch):
        if not self.restricted:
            return True
        return punch.person_id in self.person_ids or (punch.shift_id and punch.shift.site_id in self.site_ids)

    # -- queryset filters --

    def filter_people(self, queryset):
        return queryset if not self.restricted else queryset.filter(pk__in=self.person_ids)

    def filter_sites(self, queryset):
        return queryset if not self.restricted else queryset.filter(pk__in=self.site_ids)

    def filter_branches(self, queryset):
        if not self.restricted:
            return queryset
        return queryset.filter(Q(pk__in=self.branch_ids) | Q(sites__id__in=self.site_ids)).distinct()

    def filter_clients(self, queryset):
        if not self.restricted:
            return queryset
        return queryset.filter(Q(pk__in=self.client_ids) | Q(sites__id__in=self.site_ids)).distinct()

    def filter_shifts(self, queryset):
        if not self.restricted:
            return queryset
        # Posts at the covered sites first: an unfilled post has no officer, and filling it is
        # the reason a dispatcher is looking at the page.
        return queryset.filter(Q(site_id__in=self.site_ids) | Q(officer_id__in=self.person_ids))

    def filter_punches(self, queryset):
        if not self.restricted:
            return queryset
        return queryset.filter(Q(person_id__in=self.person_ids) | Q(shift__site_id__in=self.site_ids))

    def filter_by_person(self, queryset):
        """A child record of a personnel file (credential, training row, document)."""
        if not self.restricted:
            return queryset
        return queryset.filter(person_id__in=self.person_ids)

    def filter_adjustments(self, queryset):
        """Correction requests follow the punch they describe, not the person who asked."""
        if not self.restricted:
            return queryset
        return queryset.filter(
            Q(punch__person_id__in=self.person_ids) | Q(punch__shift__site_id__in=self.site_ids))


def for_membership(membership):
    if membership is None or membership.role not in SCOPE_CAPABLE_ROLES:
        return ActorScope(membership)
    return ActorScope(membership, list(membership.authority_scopes.select_related("branch", "client", "site")))


def dispatch_recipients_for_shift(shift, person=None):
    """Sign-in ids that may decide a request for this post.

    An open-post notice sent to every manager is noise to everyone whose branch it is not, and
    the one role that must never be missed is a dispatcher who was never given a scope at all.
    Company-level roles always hear about it; a field role hears about it only when their
    grants reach the post — through the site, the branch that owns the site, the contract, or
    the branch of the officer asking.
    """
    organization = shift.organization
    recipients = set(organization.memberships.filter(
        active=True, role__in=COMPANY_MANAGING_ROLES).values_list("user_id", flat=True))
    fielded = list(organization.memberships.filter(
        active=True, role__in=SCOPE_CAPABLE_ROLES).values_list("id", "user_id"))
    if not fielded:
        return recipients
    rows_by_membership = defaultdict(list)
    for row in AuthorityScope.objects.filter(membership_id__in=[item[0] for item in fielded]).values(
            "membership_id", "branch_id", "client_id", "site_id", "follows_own_branch"):
        rows_by_membership[row["membership_id"]].append(row)
    # A dispatcher whose grant follows their own branch reaches a post by the branch on their file,
    # which is read once here for the whole set: doing it per row would be a query per membership in
    # the notice path, and this runs for every open post.
    own_branch_of = {}
    if any(item["follows_own_branch"] for group in rows_by_membership.values() for item in group):
        person_branch = dict(Person.objects.filter(organization=organization, user_id__in=[
            user_id for _membership_id, user_id in fielded]).values_list("user_id", "branch_id"))
        own_branch_of = {membership_id: person_branch.get(user_id) for membership_id, user_id in fielded}
    site = shift.site if shift.site_id else None
    site_branch_id = site.branch_id if site else None
    site_client_id = site.client_id if site else None
    person_branch_id = person.branch_id if person is not None else None
    for membership_id, user_id in fielded:
        rows = rows_by_membership.get(membership_id)
        if not rows:
            # No grants means no bound on this dispatcher's authority.
            recipients.add(user_id)
            continue
        inherited_branch_id = own_branch_of.get(membership_id)
        covered = any(
            (row["site_id"] and row["site_id"] == shift.site_id)
            or (row["client_id"] and row["client_id"] == site_client_id)
            or (row["branch_id"] and row["branch_id"] in ({site_branch_id, person_branch_id} - {None}))
            or (row["follows_own_branch"] and inherited_branch_id
                and inherited_branch_id in ({site_branch_id, person_branch_id} - {None}))
            for row in rows)
        if covered:
            recipients.add(user_id)
    return recipients


def scope_for(request):
    """The caller's authority scope, resolved at most once per request."""
    cached = getattr(request, "_authority_scope", None)
    if cached is None:
        cached = for_membership(getattr(request, "membership", None))
        request._authority_scope = cached
    return cached


def manager_recipients_by_person(organization):
    """Person id → sign-in ids of the field managers whose authority covers them.

    The reverse of every filter above: a reminder about a lapsed registration has to reach the
    supervisor who can actually fill the post, not only the compliance desk. A manager with no
    grants covers everybody, exactly as before this table existed — leaving them out would turn
    a new narrowing feature into a silent loss of notices.

    Cost is two queries plus two per scoped membership, so it is built once per organization
    per reminder pass.
    """
    fielded = list(organization.memberships.filter(active=True, role__in=SCOPE_CAPABLE_ROLES))
    roster = list(organization.people.exclude(status=Person.Status.INACTIVE).values_list("id", flat=True))
    if not fielded or not roster:
        return {}
    rows_by_membership = defaultdict(list)
    for row in AuthorityScope.objects.filter(membership__in=fielded):
        rows_by_membership[row.membership_id].append(row)
    unbounded = {membership.user_id for membership in fielded if membership.pk not in rows_by_membership}
    index = {person_id: set(unbounded) for person_id in roster}
    for membership in fielded:
        rows = rows_by_membership.get(membership.pk)
        if not rows:
            continue
        scope = ActorScope(membership, rows)
        for person_id in scope.person_ids:
            index.setdefault(person_id, set()).add(membership.user_id)
    return index
