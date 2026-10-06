from datetime import datetime, time, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from .context_processors import navigation
from .models import (
    AuditEvent, AuthorityScope, Branch, Client, DocumentType, Membership,
    OnboardingItem, OnboardingTask, Organization, Person, Shift, ShiftClaim,
    ShiftExchange, ShiftSwap, ShiftTemplate, Site, TimeOffRequest,
)


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = set()

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith("/") and not href.startswith("//"):
                self.links.add(href)


class WorkspaceCardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.cards = []
        self.card = None
        self.card_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if self.card is not None and len(self.stack) == self.card_depth:
            self.card["children"].append((tag, classes))
        if tag == "article" and "card" in classes:
            self.card = {"children": [], "primary_actions": 0}
            self.card_depth = len(self.stack) + 1
        if self.card is not None and tag in ("a", "button") and "button" in classes:
            if any("card-actions" in parent_classes for _, parent_classes in self.stack):
                self.card["primary_actions"] += 1
        if tag not in ("input", "br", "hr", "img", "meta", "link"):
            self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        if tag == "article" and self.card is not None:
            self.cards.append(self.card)
            self.card = None
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()


class WorkspaceWorkflowTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(legal_name="Workflow Security", slug="workflow")
        cls.branch = Branch.objects.create(organization=cls.org, name="North")
        cls.other_branch = Branch.objects.create(organization=cls.org, name="South")
        cls.users = {}
        cls.memberships = {}
        cls.people = {}
        for role in Membership.Role.values:
            user = get_user_model().objects.create_user(username=f"{role}@workflow.example")
            cls.users[role] = user
            cls.memberships[role] = Membership.objects.create(
                organization=cls.org, user=user, role=role,
            )
            cls.people[role] = Person.objects.create(
                organization=cls.org, user=user, branch=cls.branch,
                first_name=role, last_name="Worker",
                status=Person.Status.ACTIVE,
            )
        for role in (Membership.Role.SCHEDULER, Membership.Role.SUPERVISOR):
            AuthorityScope.objects.create(
                organization=cls.org, membership=cls.memberships[role], branch=cls.branch,
            )

    def sign_in(self, role):
        self.client.force_login(self.users[role])

    def nav_for(self, role, name):
        request = SimpleNamespace(
            resolver_match=resolve(reverse(name)), user=self.users[role],
            organization=self.org, membership=self.memberships[role],
        )
        return navigation(request)

    def test_home_is_today_and_clock_stations_are_reachable_from_setup_and_time(self):
        kiosks = reverse("clock_kiosks")
        for role in Membership.Role.values:
            with self.subTest(role=role):
                self.sign_in(role)
                home = self.client.get(reverse("dashboard"))
                self.assertTemplateUsed(home, "core/workspaces/today.html")
                from core.views import MANAGERS
                manager = role in MANAGERS
                settings_page = self.client.get(reverse("settings")).content.decode()
                self.assertEqual(kiosks in settings_page, manager)
                payroll = self.client.get(reverse("workspace_payroll"))
                if payroll.status_code == 200:
                    self.assertEqual(kiosks in payroll.content.decode(), manager)

    def test_sidebar_uses_one_menu_and_pins_personal_actions_for_every_role(self):
        for role in Membership.Role.values:
            with self.subTest(role=role):
                self.sign_in(role)
                page = self.client.get(reverse("workspace_today"))
                self.assertContains(page, 'aria-label="Main navigation"', count=1)
                sidebar = page.content.decode().split("</aside>")[0]
                self.assertEqual(sidebar.count("<nav "), 1)
                self.assertIn(reverse("clock"), sidebar)
                self.assertIn(reverse("my_shifts"), sidebar)
                self.assertNotIn(reverse("person_create"), sidebar)
                self.assertContains(page, "My work")

    def test_every_role_reaches_my_account_and_its_password_and_two_factor_pages(self):
        for role in Membership.Role.values:
            with self.subTest(role=role):
                self.sign_in(role)
                sidebar = self.client.get(reverse("workspace_today")).content.decode().split("</aside>")[0]
                self.assertIn(reverse("my_account"), sidebar)
                page = self.client.get(reverse("my_account"))
                self.assertEqual(page.status_code, 200)
                body = page.content.decode()
                for name in ("account_email", "mfa_index", "person_detail", "my_contact_edit", "availability", "text_alerts"):
                    target = reverse(name, args=[self.people[role].pk]) if name == "person_detail" else reverse(name)
                    self.assertIn(target, body)
                self.assertIn(reverse("account_set_password"), body)
                for name in ("person_detail", "my_contact_edit"):
                    target = reverse(name, args=[self.people[role].pk]) if name == "person_detail" else reverse(name)
                    self.assertEqual(self.client.get(target).status_code, 200)

    def test_an_officer_updates_their_own_contact_details_but_not_hr_fields(self):
        from .models import PersonHistory
        person = self.people[Membership.Role.OFFICER]
        self.sign_in(Membership.Role.OFFICER)
        response = self.client.post(reverse("my_contact_edit"), {
            "email": "guard@example.com", "mobile_phone": "214-555-0101", "address_line1": "1 Main",
            "city": "Dallas", "state": "TX", "postal_code": "75201",
            "emergency_contact_name": "Kin", "emergency_contact_phone": "214-555-0199",
            "first_name": "Changed", "employee_id": "HACK", "hourly_rate": "99",
        })
        self.assertRedirects(response, reverse("my_account"))
        person.refresh_from_db()
        self.assertEqual((person.email, person.city, person.emergency_contact_name), ("guard@example.com", "Dallas", "Kin"))
        self.assertEqual((person.first_name, person.employee_id, person.hourly_rate), ("officer", "", None))
        history = PersonHistory.objects.get(person=person)
        self.assertEqual(history.changed_by, self.users[Membership.Role.OFFICER])
        self.assertIn("email", history.changes)
        self.assertTrue(AuditEvent.objects.filter(action="person.updated", target_id=str(person.pk)).exists())

    def test_schedule_week_grid_puts_posts_on_the_officer_row_and_open_posts_on_top(self):
        from .services import schedule_week_start
        week = schedule_week_start()
        site = Site.objects.create(organization=self.org, client=Client.objects.create(organization=self.org, name="Grid Client"),
                                   branch=self.branch, name="Grid Tower")
        officer = self.people[Membership.Role.OFFICER]
        wednesday = timezone.make_aware(datetime.combine(week + timedelta(days=2), time(9)))
        Shift.objects.create(organization=self.org, site=site, officer=officer, starts_at=wednesday,
                             ends_at=wednesday + timedelta(hours=8), status=Shift.Status.PUBLISHED, post_name="Lobby")
        Shift.objects.create(organization=self.org, site=site, starts_at=wednesday + timedelta(days=1),
                             ends_at=wednesday + timedelta(days=1, hours=8), status=Shift.Status.PUBLISHED, post_name="Gate")
        self.sign_in(Membership.Role.SCHEDULER)
        page = self.client.get(reverse("schedule"))
        grid = page.context["grid"]
        self.assertEqual([day["date"] for day in grid["days"]], [week + timedelta(days=offset) for offset in range(7)])
        self.assertEqual([len(cell["shifts"]) for cell in grid["open"]], [0, 0, 0, 1, 0, 0, 0])
        row = next(row for row in grid["rows"] if row["person"] == officer)
        self.assertEqual([len(cell["shifts"]) for cell in row["cells"]], [0, 0, 1, 0, 0, 0, 0])
        self.assertEqual(row["hours"], 8)
        # Unfiltered, people with no posts still get a row: they are who the post can go to.
        self.assertIn(self.people[Membership.Role.HR], [row["person"] for row in grid["rows"]])
        self.assertNotIn(self.people[Membership.Role.HR], [row["person"] for row in self.client.get(reverse("schedule") + "?show=open").context["grid"]["rows"]])
        body = page.content.decode()
        self.assertLess(body.index("Open shifts"), body.index(f"{officer.last_name}, {officer.first_name}"))
        self.assertIn(f"{reverse('shift_create')}?date={week:%Y-%m-%d}&amp;officer={officer.pk}", body)
        listed = self.client.get(reverse("schedule") + "?layout=list")
        self.assertIsNone(listed.context["grid"])
        self.assertContains(listed, "Cancel this post")

    def test_the_grid_plus_prefills_the_day_and_officer_and_saving_returns_to_that_week(self):
        from .services import schedule_week_start
        officer = self.people[Membership.Role.OFFICER]
        next_week = schedule_week_start(offset=1)
        self.sign_in(Membership.Role.SCHEDULER)
        form = self.client.get(reverse("shift_create") + f"?date={next_week:%Y-%m-%d}&officer={officer.pk}").context["form"]
        self.assertEqual(form.initial["starts_at"], f"{next_week:%Y-%m-%d}T08:00")
        self.assertEqual(form.initial["officer"], officer.pk)
        outsider = Person.objects.create(organization=self.org, branch=self.other_branch, first_name="Out", last_name="Side",
                                         status=Person.Status.ACTIVE)
        form = self.client.get(reverse("shift_create") + f"?officer={outsider.pk}&date=nonsense").context["form"]
        self.assertIsNone(form.initial["officer"])
        self.assertIsNone(form.initial["starts_at"])
        site = Site.objects.create(organization=self.org, client=Client.objects.create(organization=self.org, name="Next Client"),
                                   branch=self.branch, name="Next Tower")
        response = self.client.post(reverse("shift_create"), {
            "site": site.pk, "starts_at": f"{next_week:%Y-%m-%d}T08:00", "ends_at": f"{next_week:%Y-%m-%d}T16:00",
            "status": Shift.Status.DRAFT,
        })
        self.assertRedirects(response, reverse("schedule") + "?week=1")

    def test_the_schedule_week_starts_on_the_payroll_workweek_day(self):
        from .models import TimePolicy
        from .services import schedule_week_start
        TimePolicy.objects.create(organization=self.org, timezone="America/Chicago", workweek_start=6)
        week = schedule_week_start(organization=self.org)
        self.assertEqual(week.weekday(), 6)
        self.assertLessEqual(week, timezone.localdate())
        self.sign_in(Membership.Role.SCHEDULER)
        grid = self.client.get(reverse("schedule")).context["grid"]
        self.assertEqual([day["date"].weekday() for day in grid["days"]], [6, 0, 1, 2, 3, 4, 5])
        self.assertEqual(grid["days"][0]["date"], week)
        site = Site.objects.create(organization=self.org, client=Client.objects.create(organization=self.org, name="Sun Client"),
                                   branch=self.branch, name="Sun Tower")
        saturday = week + timedelta(days=6)
        response = self.client.post(reverse("shift_create"), {
            "site": site.pk, "starts_at": f"{saturday:%Y-%m-%d}T08:00", "ends_at": f"{saturday:%Y-%m-%d}T16:00",
            "status": Shift.Status.DRAFT,
        })
        # Saturday closes the Sunday-started week, so the post is on this week's page, not next week's.
        self.assertRedirects(response, reverse("schedule"))

    def test_my_shifts_week_shows_only_my_posts_leave_and_open_work_i_can_take(self):
        from .models import AvailabilityRule, TimePolicy
        from .services import schedule_week_start
        TimePolicy.objects.create(organization=self.org, timezone="America/Chicago", workweek_start=6)
        week = schedule_week_start(offset=1, organization=self.org)
        site = Site.objects.create(organization=self.org, client=Client.objects.create(organization=self.org, name="Mine Client"),
                                   branch=self.branch, name="Mine Tower")
        officer = self.people[Membership.Role.OFFICER]
        colleague = self.people[Membership.Role.SUPERVISOR]

        def at(day, hour):
            return timezone.make_aware(datetime.combine(week + timedelta(days=day), time(hour)))

        mine = Shift.objects.create(organization=self.org, site=site, officer=officer, starts_at=at(1, 9), ends_at=at(1, 17),
                                    status=Shift.Status.PUBLISHED, post_name="Lobby")
        Shift.objects.create(organization=self.org, site=site, officer=officer, starts_at=at(2, 9), ends_at=at(2, 17),
                             status=Shift.Status.CANCELLED)
        Shift.objects.create(organization=self.org, site=site, officer=colleague, starts_at=at(3, 9), ends_at=at(3, 17),
                             status=Shift.Status.PUBLISHED, post_name="Their post")
        Shift.objects.create(organization=self.org, site=site, starts_at=at(3, 18), ends_at=at(3, 23),
                             status=Shift.Status.PUBLISHED, post_name="Open gate")
        Shift.objects.create(organization=self.org, site=site, starts_at=at(5, 9), ends_at=at(5, 17),
                             status=Shift.Status.PUBLISHED, post_name="Blocked by leave")
        TimeOffRequest.objects.create(organization=self.org, person=officer, starts_at=at(5, 0), ends_at=at(6, 0),
                                      status=TimeOffRequest.Status.APPROVED)
        AvailabilityRule.objects.create(organization=self.org, person=officer, weekday=(week + timedelta(days=4)).weekday(),
                                        starts_at=time(18), ends_at=time(2))
        self.sign_in(Membership.Role.OFFICER)
        page = self.client.get(reverse("my_shifts") + "?week=1")
        days = page.context["week"]["days"]
        self.assertEqual([day["date"].weekday() for day in days], [6, 0, 1, 2, 3, 4, 5])
        self.assertEqual([[shift.pk for shift in day["shifts"]] for day in days], [[], [mine.pk], [], [], [], [], []])
        self.assertEqual(page.context["week"]["hours"], 8)
        self.assertEqual([day["open_count"] for day in days], [0, 0, 0, 1, 0, 0, 0])
        self.assertEqual([bool(day["leave"]) for day in days], [False] * 5 + [True, False])
        self.assertEqual([len(day["availability"]) for day in days], [0, 0, 0, 0, 1, 0, 0])
        body = page.content.decode()
        self.assertIn(f'href="#shift-{mine.pk}"', body)
        self.assertNotIn("Their post", body)
        self.assertNotIn(reverse("shift_edit", args=[mine.pk]), body)
        self.assertContains(page, "1 open post you can work")
        self.assertContains(page, "Approved leave")
        clamped = self.client.get(reverse("my_shifts") + "?week=99").context["week"]
        self.assertEqual(clamped["offset"], 8)
        self.assertFalse(clamped["can_ahead"])
        self.assertEqual(self.client.get(reverse("my_shifts") + "?week=x").context["week"]["offset"], 0)

    def test_visible_workspace_links_lead_to_accessible_get_destinations(self):
        for role in Membership.Role.values:
            self.sign_in(role)
            nav = self.nav_for(role, "workspace_today")
            for workspace in nav["workspaces"]:
                with self.subTest(role=role, workspace=workspace["label"]):
                    page = self.client.get(workspace["url"])
                    self.assertEqual(page.status_code, 200)
                    parser = LinkParser()
                    parser.feed(page.content.decode())
                    for href in sorted(parser.links):
                        with self.subTest(href=href):
                            result = self.client.get(href)
                            self.assertIn(result.status_code, (200, 302))

    def test_workspace_cards_keep_their_primary_action_in_a_final_footer(self):
        self.scheduling_post()
        Person.objects.filter(pk=self.people[Membership.Role.OFFICER].pk).update(
            status=Person.Status.ONBOARDING,
        )
        for role in Membership.Role.values:
            self.sign_in(role)
            for workspace in self.nav_for(role, "workspace_today")["workspaces"]:
                with self.subTest(role=role, workspace=workspace["label"]):
                    page = self.client.get(workspace["url"])
                    self.assertEqual(page.status_code, 200)
                    parser = WorkspaceCardParser()
                    parser.feed(page.content.decode())
                    self.assertTrue(parser.cards)
                    for card in parser.cards:
                        footers = [
                            index for index, (_, classes) in enumerate(card["children"])
                            if "card-actions" in classes
                        ]
                        # An auditor's rules summary is intentionally read-only.
                        if not footers:
                            self.assertFalse(any(tag in ("a", "form") for tag, _ in card["children"]))
                            continue
                        self.assertEqual(footers, [len(card["children"]) - 1])
                        self.assertEqual(card["primary_actions"], 1)
                        self.assertFalse(any(tag in ("a", "form") for tag, _ in card["children"]))

    def test_employee_today_uses_real_checklist_states_and_signing_destination(self):
        person = self.people[Membership.Role.OFFICER]
        document_type = DocumentType.objects.create(organization=self.org, name="Agreement")
        for code, kind, status in (
            ("sign", OnboardingItem.Kind.SIGNATURE, OnboardingTask.Status.OPEN),
            ("done", OnboardingItem.Kind.TASK, OnboardingTask.Status.DONE),
            ("waived", OnboardingItem.Kind.TASK, OnboardingTask.Status.WAIVED),
        ):
            item = OnboardingItem.objects.create(
                organization=self.org, name=code, code=code, kind=kind,
                document_type=document_type if kind == OnboardingItem.Kind.SIGNATURE else None,
                signing_template_id=1 if kind == OnboardingItem.Kind.SIGNATURE else None,
            )
            OnboardingTask.objects.create(
                organization=self.org, person=person, item=item, status=status,
                due_on=timezone.localdate() - timedelta(days=1),
            )
        self.sign_in(Membership.Role.OFFICER)
        page = self.client.get(reverse("workspace_today"))
        self.assertEqual(page.context["pending_onboarding"], 1)
        self.assertEqual(page.context["pending_signatures"], 1)
        self.assertEqual(page.context["onboarding_overdue"], 1)
        self.assertContains(page, f'href="{reverse("my_onboarding")}"')
        for name in ("my_documents", "availability", "my_time_off", "open_posts", "text_alerts"):
            self.assertContains(page, f'href="{reverse(name)}"')

    def test_manager_keeps_personal_work_beside_team_actions(self):
        self.sign_in(Membership.Role.OWNER)
        page = self.client.get(reverse("workspace_today"))
        self.assertContains(page, "Team actions")
        self.assertContains(page, "Clock in/out")
        self.assertContains(page, "Open my checklist")

    def test_unlinked_login_gets_an_explicit_explanation(self):
        Person.objects.filter(pk=self.people[Membership.Role.OWNER].pk).update(user=None)
        self.sign_in(Membership.Role.OWNER)
        page = self.client.get(reverse("workspace_today"))
        self.assertContains(page, "Your login is not linked to a personnel record")
        self.assertNotContains(page, "Clock in/out")

    def test_supervisor_cannot_see_company_payroll_and_dispatcher_has_no_time_review_link(self):
        self.sign_in(Membership.Role.SUPERVISOR)
        page = self.client.get(reverse("workspace_payroll"))
        self.assertContains(page, "<h1>Time review</h1>")
        self.assertNotContains(page, f'href="{reverse("payroll")}"')
        self.assertNotContains(page, "Latest payroll")
        self.assertEqual(page.context["runs"], [])
        self.sign_in(Membership.Role.SCHEDULER)
        page = self.client.get(reverse("workspace_today"))
        self.assertNotContains(page, f'href="{reverse("time_review")}"')

    def test_only_one_workspace_is_active_on_shared_management_routes(self):
        for name, expected in (("open_posts", "Schedule"), ("availability", "People"),
                               ("clock", "Today"), ("dashboard", "Today")):
            with self.subTest(name=name):
                nav = self.nav_for(Membership.Role.OWNER, name)
                self.assertEqual(nav["active_workspace"], expected)
                self.assertEqual(sum(item["active"] for item in nav["workspaces"]), 1)
        nav = self.nav_for(Membership.Role.OFFICER, "open_posts")
        self.assertEqual(nav["active_workspace"], "Today")

    def test_settings_is_open_on_its_own_pages(self):
        self.sign_in(Membership.Role.OWNER)
        page = self.client.get(reverse("pay_codes"))
        self.assertContains(page, '<details class="settings-group" open>')
        self.assertTrue(page.context["settings_active"])
        self.assertIsNone(page.context["active_workspace"])
        self.assertEqual(sum(item["active"] for item in page.context["settings_nav"]), 1)

    def test_onboarding_directory_filter_really_filters_and_preserves_search(self):
        Person.objects.create(
            organization=self.org, first_name="New", last_name="Hire",
            status=Person.Status.ONBOARDING,
        )
        self.sign_in(Membership.Role.OWNER)
        page = self.client.get(reverse("people"), {"status": "onboarding", "q": "New"})
        self.assertEqual(page.context["paginator"].count, 1)
        self.assertContains(page, '<option value="onboarding" selected>')
        self.assertNotContains(page, "owner Worker")
        page = self.client.get(reverse("people"), {"status": "not-a-status"})
        self.assertEqual(page.context["status"], "")

    def test_schedule_counts_all_open_posts_not_just_preview_and_respects_scope(self):
        client = Client.objects.create(organization=self.org, name="Contract")
        site = Site.objects.create(organization=self.org, client=client, branch=self.branch, name="Gate")
        hidden_site = Site.objects.create(
            organization=self.org, client=client, branch=self.other_branch, name="Hidden gate",
        )
        now = timezone.now()
        for index in range(18):
            Shift.objects.create(
                organization=self.org, site=site, starts_at=now,
                ends_at=now + timedelta(hours=8), post_name=f"Gate {index}",
            )
        Shift.objects.create(
            organization=self.org, site=hidden_site, starts_at=now,
            ends_at=now + timedelta(hours=8),
        )
        for location in (site, hidden_site):
            ShiftTemplate.objects.create(
                organization=self.org, site=location, name=location.name,
                start_time=time(8), end_time=time(16),
                series_start=timezone.localdate(),
            )
        self.sign_in(Membership.Role.SCHEDULER)
        page = self.client.get(reverse("workspace_schedule"))
        self.assertEqual(page.context["open_posts_count"], 18)
        self.assertEqual(len(page.context["open_posts"]), 15)
        self.assertEqual(page.context["recurring_series_count"], 1)

    def test_reports_shows_real_audit_timestamp_and_capture_uses_post(self):
        AuditEvent.objects.create(
            organization=self.org, actor=self.users[Membership.Role.OWNER],
            action="workflow.checked", target_type="organization", target_id=str(self.org.pk),
        )
        self.sign_in(Membership.Role.OWNER)
        page = self.client.get(reverse("workspace_reports"))
        self.assertContains(page, "workflow.checked")
        self.assertEqual(len(page.context["audit_events"]), 1)
        self.assertContains(page, f'<form method="post" action="{reverse("report_capture")}">')
        self.assertNotContains(page, f'href="{reverse("report_capture")}"')
        self.sign_in(Membership.Role.SUPERVISOR)
        page = self.client.get(reverse("workspace_reports"))
        self.assertNotContains(page, "Save snapshot")
        self.assertEqual(page.context["audit_events"], [])

    def test_scoped_people_counts_do_not_include_another_branch(self):
        Person.objects.create(
            organization=self.org, branch=self.other_branch, first_name="Hidden", last_name="Hire",
            status=Person.Status.ONBOARDING,
        )
        self.sign_in(Membership.Role.SUPERVISOR)
        page = self.client.get(reverse("workspace_people"))
        self.assertEqual(page.context["onboarding_count"], 0)
        self.assertNotContains(page, "Hidden Hire")

    def scheduling_post(self, *, branch=None, starts_at=None, officer=None, organization=None):
        organization = organization or self.org
        branch = branch or self.branch
        client, _ = Client.objects.get_or_create(organization=organization, name="Queue client")
        site, _ = Site.objects.get_or_create(
            organization=organization, client=client, branch=branch, name=f"{branch.name} queue gate",
        )
        starts_at = starts_at or timezone.now() + timedelta(days=10)
        return Shift.objects.create(
            organization=organization, site=site, officer=officer,
            status=Shift.Status.PUBLISHED, starts_at=starts_at, ends_at=starts_at + timedelta(hours=8),
        )

    def test_open_post_request_is_prominent_beyond_current_week_and_shared_with_today(self):
        post = self.scheduling_post()
        claim = ShiftClaim.objects.create(
            organization=self.org, shift=post, officer=self.people[Membership.Role.OFFICER],
            note="I can cover the gate",
        )
        for role in (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.SCHEDULER,
                     Membership.Role.SUPERVISOR, Membership.Role.HR):
            with self.subTest(role=role):
                self.sign_in(role)
                page = self.client.get(reverse("workspace_schedule"))
                self.assertEqual(page.context["scheduling_actions"]["claim_count"], 1)
                self.assertEqual(page.context["scheduling_actions"]["review_count"], 1)
                self.assertContains(page, "I can cover the gate")
                self.assertContains(page, f'href="{reverse("shift_requests", args=[post.pk])}"')
                text = page.content.decode()
                self.assertLess(text.index("Scheduling action queue"), text.index("<h2>Open posts</h2>"))
                today = self.client.get(reverse("workspace_today"))
                self.assertEqual(today.context["scheduling_actions"]["claim_count"], 1)
                self.assertContains(today, f'href="{reverse("workspace_schedule")}#schedule-actions"')
        claim.status = ShiftClaim.Status.APPROVED
        claim.save()
        page = self.client.get(reverse("workspace_schedule"))
        self.assertEqual(page.context["scheduling_actions"]["total_count"], 0)
        self.assertContains(page, "No pending scheduling requests")

    def test_scheduling_queue_obeys_shift_scope_person_scope_and_tenant(self):
        visible = self.scheduling_post()
        hidden = self.scheduling_post(branch=self.other_branch)
        other_org = Organization.objects.create(legal_name="Foreign", slug="queue-foreign")
        other_person = Person.objects.create(organization=other_org, first_name="Foreign", last_name="Worker")
        foreign_branch = Branch.objects.create(organization=other_org, name="Foreign branch")
        foreign = self.scheduling_post(organization=other_org, branch=foreign_branch)
        for post, person, note in (
            (visible, self.people[Membership.Role.OFFICER], "Visible claim"),
            (hidden, self.people[Membership.Role.OFFICER], "Hidden claim"),
            (foreign, other_person, "Foreign claim"),
        ):
            ShiftClaim.objects.create(organization=post.organization, shift=post, officer=person, note=note)
        hidden_person = Person.objects.create(
            organization=self.org, branch=self.other_branch, first_name="Hidden", last_name="Worker",
        )
        for person in (self.people[Membership.Role.OFFICER], hidden_person):
            TimeOffRequest.objects.create(
                organization=self.org, person=person, starts_at=visible.starts_at,
                ends_at=visible.ends_at, reason=f"Leave for {person.first_name}",
            )
        self.sign_in(Membership.Role.SCHEDULER)
        page = self.client.get(reverse("workspace_schedule"))
        actions = page.context["scheduling_actions"]
        self.assertEqual(actions["claim_count"], 1)
        self.assertEqual(actions["leave_count"], 1)
        self.assertNotContains(page, "Hidden claim")
        self.assertNotContains(page, "Foreign claim")
        self.assertNotContains(page, "Leave for Hidden")
        self.assertContains(page, "Visible claim")

    def test_queue_distinguishes_employee_consent_from_manager_review(self):
        requester = self.people[Membership.Role.OFFICER]
        colleague = self.people[Membership.Role.SUPERVISOR]
        post = self.scheduling_post(officer=requester)
        hidden = self.scheduling_post(branch=self.other_branch)
        ShiftSwap.objects.create(
            organization=self.org, shift=post, requester=requester, replacement=colleague,
            status=ShiftSwap.Status.OFFERED,
        )
        ShiftExchange.objects.create(
            organization=self.org, initiator=requester, partner=colleague, initiator_shift=post,
            status=ShiftExchange.Status.AGREED,
        )
        # Both halves of a trade must be within the dispatcher's scope.
        ShiftExchange.objects.create(
            organization=self.org, initiator=colleague, partner=requester, initiator_shift=post,
            partner_shift=hidden, status=ShiftExchange.Status.AGREED, note="Hidden half of trade",
        )
        self.sign_in(Membership.Role.SCHEDULER)
        page = self.client.get(reverse("workspace_schedule"))
        actions = page.context["scheduling_actions"]
        self.assertEqual(actions["move_count"], 2)
        self.assertEqual(actions["review_count"], 1)
        self.assertEqual(actions["waiting_count"], 1)
        self.assertFalse(actions["rows"][0]["waiting"])
        self.assertContains(page, "View consent status")
        self.assertNotContains(page, "Hidden half of trade")

    def test_queue_paginates_without_hiding_total_and_retains_past_unresolved_requests(self):
        post = self.scheduling_post(starts_at=timezone.now() - timedelta(days=2))
        for index in range(12):
            person = Person.objects.create(
                organization=self.org, first_name=f"Applicant {index}", last_name="Worker",
            )
            ShiftClaim.objects.create(organization=self.org, shift=post, officer=person)
        self.sign_in(Membership.Role.OWNER)
        page = self.client.get(reverse("workspace_schedule"))
        self.assertEqual(page.context["scheduling_actions"]["claim_count"], 12)
        self.assertEqual(len(page.context["action_queue"]), 10)
        self.assertContains(page, "Past date")
        page = self.client.get(reverse("workspace_schedule") + "?page=2")
        self.assertEqual(len(page.context["action_queue"]), 2)
        self.assertEqual(page.context["scheduling_actions"]["claim_count"], 12)
        self.sign_in(Membership.Role.OFFICER)
        self.assertEqual(self.client.get(reverse("workspace_schedule")).status_code, 403)
        page = self.client.get(reverse("workspace_today"))
        self.assertNotContains(page, "Scheduling requests")


class AboutPageTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(legal_name="About Security", slug="about")
        cls.officer = get_user_model().objects.create_user(username="officer@about.example")
        Membership.objects.create(organization=cls.org, user=cls.officer, role=Membership.Role.OFFICER)
        cls.stranger = get_user_model().objects.create_user(username="nobody@about.example")

    def test_about_shows_version_publisher_copyright_and_shipped_license_texts(self):
        from django.conf import settings
        self.assertEqual(settings.APP_VERSION, "1.0.0")
        self.client.force_login(self.officer)
        response = self.client.get(reverse("about"))
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn("Version 1.0.0", page)
        self.assertIn("Bachman Group, LLC", page)
        self.assertIn("PO Box 4, Lancaster, TX 75146", page)
        self.assertIn('href="https://bachman.xyz"', page)
        self.assertIn(f"Copyright &copy; {timezone.localdate().year}", page)
        names = {item["name"] for item in response.context["packages"]}
        self.assertTrue({"Django", "django-allauth", "redis"} <= names, names)
        django_row = next(item for item in response.context["packages"] if item["name"] == "Django")
        self.assertTrue(any("Django Software Foundation" in text["text"] for text in django_row["texts"]))
        # Django admin ships jQuery in the static files we serve, so its license must travel with it.
        self.assertTrue(any("jquery" in text["name"].lower() for text in django_row["texts"]))
        self.assertIn("PDF.js", page)
        self.assertIn("LICENSE_FOXIT", page)
        self.assertIn("DocuSeal", page)
        self.assertIn('href="/about/"', page)

    def test_about_is_open_to_any_signed_in_user_but_not_anonymous_visitors(self):
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(reverse("about")).status_code, 200)
        self.client.logout()
        response = self.client.get(reverse("about"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_sign_in_page_carries_version_and_copyright(self):
        page = self.client.get(reverse("account_login")).content.decode()
        self.assertIn("v1.0.0", page)
        self.assertIn("Bachman Group, LLC", page)
