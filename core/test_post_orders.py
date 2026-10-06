from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import ClientForm, ShiftForm, ShiftTemplateForm, SiteForm
from .models import (
    AuditEvent, AuthorityScope, Branch, Client, Membership, Organization, Person,
    Shift, ShiftTemplate, Site,
)
from .services import apply_recurring_plan
from .scope import for_membership
from .views import _scoped_form


class PostOrdersTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            legal_name="Orders", display_name="Orders", slug="orders",
            default_post_orders="Company orders\nRecord every incident.")
        self.owner = get_user_model().objects.create_user(username="orders-owner")
        self.worker = get_user_model().objects.create_user(username="orders-worker")
        Membership.objects.create(organization=self.org, user=self.owner, role=Membership.Role.OWNER)
        Membership.objects.create(organization=self.org, user=self.worker, role=Membership.Role.OFFICER)
        self.person = Person.objects.create(
            organization=self.org, user=self.worker, first_name="Our", last_name="Officer")
        self.customer = Client.objects.create(organization=self.org, name="Our client")
        self.site = Site.objects.create(organization=self.org, client=self.customer, name="Gate", address="1 Main St")
        self.start = timezone.now() + timedelta(days=2)
        self.shift = Shift.objects.create(
            organization=self.org, site=self.site, officer=self.person,
            starts_at=self.start, ends_at=self.start + timedelta(hours=8), status=Shift.Status.PUBLISHED)
        self.client.force_login(self.owner)

    def test_resolution_uses_most_specific_nonblank_orders(self):
        self.assertEqual(self.shift.post_orders_resolution, (self.org.default_post_orders, "company"))
        self.customer.default_post_orders = "Client orders"
        self.assertEqual(self.shift.post_orders_resolution, ("Client orders", "client"))
        self.site.default_post_orders = "Site orders"
        self.assertEqual(self.shift.post_orders_resolution, ("Site orders", "site"))
        self.shift.post_orders = "Post orders"
        self.assertEqual(self.shift.post_orders_resolution, ("Post orders", "post"))
        self.shift.post_orders = " \n "
        self.assertEqual(self.shift.post_orders_resolution, ("Site orders", "site"))
        self.site.default_post_orders = ""
        self.customer.default_post_orders = ""
        self.org.default_post_orders = ""
        self.assertEqual(self.shift.post_orders_resolution, ("", ""))

    def test_create_and_edit_forms_prefill_inherited_orders(self):
        targets = (
            (ClientForm, {}, "default_post_orders"),
            (SiteForm, {"client": self.customer.pk}, "default_post_orders"),
            (ShiftForm, {"site": self.site.pk}, "post_orders"),
            (ShiftTemplateForm, {"site": self.site.pk}, "post_orders"),
        )
        for form_class, initial, field in targets:
            with self.subTest(form=form_class.__name__):
                form = _scoped_form(form_class, self.org, initial=initial)
                self.assertEqual(form[field].value(), self.org.default_post_orders)
        page = self.client.get(reverse("shift_edit", args=[self.shift.pk]))
        self.assertEqual(page.context["form"]["post_orders"].value(), self.org.default_post_orders)
        self.assertContains(page, 'id="post-orders-defaults"')

    def test_unchanged_client_prefill_inherits_and_edits_create_override(self):
        data = {"name": "Created client", "default_post_orders": self.org.default_post_orders, "active": "on"}
        self.assertRedirects(self.client.post(reverse("client_create"), data), reverse("locations"))
        customer = Client.objects.get(name="Created client")
        self.assertEqual(customer.default_post_orders, "")
        self.org.default_post_orders = "Revised company orders"
        self.org.save()
        customer.refresh_from_db()
        self.assertEqual(customer.effective_post_orders, "Revised company orders")
        data["default_post_orders"] = "Edited client instructions"
        self.assertRedirects(self.client.post(reverse("client_edit", args=[customer.pk]), data), reverse("locations"))
        customer.refresh_from_db()
        self.assertEqual(customer.effective_post_orders, "Edited client instructions")
        data["default_post_orders"] = ""
        self.client.post(reverse("client_edit", args=[customer.pk]), data)
        customer.refresh_from_db()
        self.assertEqual(customer.default_post_orders, "")
        self.assertEqual(customer.effective_post_orders, "Revised company orders")

    def test_site_prefill_tracks_selected_client_and_saves_blank_when_unchanged(self):
        self.customer.default_post_orders = "Client baseline"
        self.customer.save()
        form = _scoped_form(SiteForm, self.org, {
            "client": str(self.customer.pk), "name": "New gate", "address": "2 Main St",
            "geofence_radius_meters": "200", "active": "on", "default_post_orders": "Client baseline",
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["default_post_orders"], "")
        self.assertEqual(form.post_orders_defaults["choices"][str(self.customer.pk)]["text"], "Client baseline")

    def test_saving_a_stale_prefill_does_not_override_new_defaults(self):
        page = self.client.get(reverse("client_create"))
        signature = page.context["form"]["post_orders_inheritance"].value()
        original = self.org.default_post_orders
        self.org.default_post_orders = "Updated while the form was open"
        self.org.save()
        self.client.post(reverse("client_create"), {
            "name": "Stale editor", "default_post_orders": original,
            "post_orders_inheritance": signature, "active": "on",
        })
        customer = Client.objects.get(name="Stale editor")
        self.assertEqual(customer.default_post_orders, "")
        self.assertEqual(customer.effective_post_orders, "Updated while the form was open")

    def test_post_editor_keeps_defaults_live_and_preserves_explicit_override(self):
        self.site.default_post_orders = "Gate orders"
        self.site.save()
        data = {
            "site": str(self.site.pk), "officer": str(self.person.pk), "status": Shift.Status.DRAFT,
            "starts_at": self.start.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": (self.start + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M"),
            "post_orders": "Gate orders",
        }
        form = _scoped_form(ShiftForm, self.org, data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["post_orders"], "")
        data["post_orders"] = "Gate orders with a one-off exception"
        form = _scoped_form(ShiftForm, self.org, data)
        self.assertTrue(form.is_valid(), form.errors)
        post = form.save()
        self.site.default_post_orders = "Changed gate orders"
        self.site.save()
        post.refresh_from_db()
        self.assertEqual(post.effective_post_orders, "Gate orders with a one-off exception")
        form = _scoped_form(ShiftForm, self.org, instance=post)
        self.assertEqual(form["post_orders"].value(), post.post_orders)

    def test_recurring_posts_keep_inheritance_unless_the_series_has_orders(self):
        day = timezone.localtime(self.start).date()
        template = ShiftTemplate.objects.create(
            organization=self.org, site=self.site, name="Recurring gate",
            start_time=time(8), end_time=time(16), weekdays=[day.weekday()], series_start=day)
        plan = apply_recurring_plan(template, day, day, Shift.Status.DRAFT, actor=self.owner)
        self.assertEqual(plan["count"], 1)
        post = template.shifts.get()
        self.assertEqual(post.post_orders, "")
        self.assertEqual(post.effective_post_orders, self.org.default_post_orders)
        template.post_orders = "Recurring exception"
        template.save()
        self.assertEqual(template.effective_post_orders, "Recurring exception")
        plan = apply_recurring_plan(template, day + timedelta(days=7), day + timedelta(days=7),
                                    Shift.Status.DRAFT, actor=self.owner)
        self.assertEqual(plan["count"], 1)
        self.assertEqual(template.shifts.order_by("starts_at").last().post_orders, "Recurring exception")

    def test_employee_brief_and_open_posts_use_inherited_orders_and_escape_text(self):
        self.org.default_post_orders = "Company <script>unsafe</script>\nSecond line"
        self.org.save()
        self.client.force_login(self.worker)
        page = self.client.get(reverse("my_shifts"))
        self.assertContains(page, "Company &lt;script&gt;unsafe&lt;/script&gt;")
        self.assertContains(page, "Orders from the company.")
        self.assertNotContains(page, "No post orders recorded")
        self.shift.officer = None
        self.shift.save()
        self.assertContains(self.client.get(reverse("open_posts")), "Orders from company")

    def test_company_editor_is_privileged_and_audited(self):
        self.assertRedirects(
            self.client.post(reverse("company_post_orders"), {"default_post_orders": "New baseline"}),
            reverse("settings"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.default_post_orders, "New baseline")
        self.assertTrue(AuditEvent.objects.filter(action="organization.post_orders_updated").exists())
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse("company_post_orders")).status_code, 403)

    def test_prefill_data_excludes_other_tenants_and_outside_scopes(self):
        other = Organization.objects.create(legal_name="Other", slug="other-orders", default_post_orders="FOREIGN ORDERS")
        foreign_client = Client.objects.create(organization=other, name="Foreign", default_post_orders="PRIVATE CLIENT")
        foreign_site = Site.objects.create(organization=other, client=foreign_client, name="Foreign", address="Elsewhere")
        branch = Branch.objects.create(organization=self.org, name="Covered")
        self.site.branch = branch
        self.site.save()
        outside = Site.objects.create(organization=self.org, client=self.customer, name="Outside",
                                      address="Outside", default_post_orders="OUTSIDE ORDERS")
        membership = Membership.objects.create(
            organization=self.org, user=get_user_model().objects.create_user(username="scoped-orders"),
            role=Membership.Role.SCHEDULER)
        AuthorityScope.objects.create(organization=self.org, membership=membership, branch=branch)
        form = _scoped_form(ShiftForm, self.org, scope=for_membership(membership))
        choices = form.post_orders_defaults["choices"]
        self.assertIn(str(self.site.pk), choices)
        self.assertNotIn(str(outside.pk), choices)
        self.assertNotIn(str(foreign_site.pk), choices)
