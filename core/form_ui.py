from django import forms
from django.core import signing
from django.forms.models import ModelChoiceIterator, ModelChoiceIteratorValue
from django.forms.renderers import DjangoTemplates
from django.template.loader import get_template

from .models import Site


class GroupedSiteSelect(forms.Select):
    def optgroups(self, name, value, attrs=None):
        groups = []
        clients = {}
        for _, options, _ in super().optgroups(name, value, attrs):
            for option in options:
                choice = option["value"]
                site = getattr(choice, "instance", None) if isinstance(choice, ModelChoiceIteratorValue) else None
                if isinstance(site, Site):
                    option["label"] = site.name
                    client_id = site.client.pk
                    if client_id not in clients:
                        clients[client_id] = []
                        groups.append((site.client.name, clients[client_id], len(groups)))
                    clients[client_id].append(option)
                else:
                    groups.append((None, [option], len(groups)))
        return groups


class WorkflowFormRenderer(DjangoTemplates):
    def get_template(self, template_name):
        if template_name.startswith("core/"):
            return get_template(template_name)
        return super().get_template(template_name)


class WorkflowBoundField(forms.BoundField):
    def css_classes(self, extra_classes=None):
        classes = ["form-field"]
        if isinstance(self.field.widget, (forms.Textarea, forms.CheckboxSelectMultiple, forms.SelectMultiple)):
            classes.append("form-field-wide")
        if isinstance(self.field.widget, forms.CheckboxInput):
            classes.append("form-field-checkbox")
        if self.errors:
            classes.append("form-field-invalid")
        if isinstance(extra_classes, str):
            classes.extend(extra_classes.split())
        elif extra_classes:
            classes.extend(extra_classes)
        return super().css_classes(" ".join(classes))


class WorkflowFormMixin(forms.BaseForm):
    template_name_div = "core/_form_fields.html"
    bound_field_class = WorkflowBoundField
    default_renderer = WorkflowFormRenderer
    field_sections: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if (isinstance(field, forms.ModelChoiceField)
                    and not isinstance(field, forms.ModelMultipleChoiceField)
                    and isinstance(field.widget, forms.Select)
                    and field.queryset is not None and field.queryset.model is Site):
                widget = GroupedSiteSelect(attrs=field.widget.attrs)
                widget.choices = ModelChoiceIterator(field)
                field.widget = widget

    @property
    def layout_sections(self):
        sections = []
        assigned = set()
        for title, names in self.field_sections:
            fields = [self[name] for name in names if name in self.fields and not self[name].is_hidden]
            if fields:
                sections.append({"title": title, "fields": fields})
                assigned.update(field.name for field in fields)
        remaining = [field for field in self.visible_fields() if field.name not in assigned]
        if remaining:
            sections.append({"title": "Details" if sections else "", "fields": remaining})
        return sections


class WorkflowForm(WorkflowFormMixin, forms.Form):
    pass


class WorkflowModelForm(WorkflowFormMixin, forms.ModelForm):
    pass


class InheritedPostOrdersForm(WorkflowModelForm):
    orders_field = "default_post_orders"
    orders_parent_field = ""
    post_orders_inheritance = forms.CharField(required=False, widget=forms.HiddenInput())

    def configure_post_orders(self, organization):
        self.orders_organization = organization
        field = self.fields[self.orders_field]
        field.widget.attrs["data-post-orders"] = ""
        field.help_text = (
            "Inherited orders are shown for editing. Leave them unchanged to keep using live defaults; "
            "edit to override at this level, or clear the box to restore inheritance."
        )
        choices = {}
        if self.orders_parent_field:
            parent_field = self.fields[self.orders_parent_field]
            if not isinstance(parent_field, forms.ModelChoiceField):
                raise TypeError("The post-orders parent must be a model choice.")
            parents = parent_field.queryset
            if parents is None:
                raise ValueError("The post-orders parent needs a scoped queryset.")
            if self.orders_parent_field == "site":
                parents = parents.select_related("client__organization")
            else:
                parents = parents.select_related("organization")
            choices = {str(parent.pk): {"text": parent.effective_post_orders, "source": parent.post_orders_source}
                       for parent in parents}
        baseline = {"text": organization.default_post_orders, "source": "company" if organization.default_post_orders.strip() else ""}
        for parent_id, default in [("", baseline), *choices.items()]:
            default["signature"] = signing.dumps(
                {"organization": str(organization.pk), "parent": parent_id,
                 "field": self.orders_field, "text": default["text"]},
                salt="post-orders-inheritance", compress=True)
        parent_id = self.data.get(self.orders_parent_field) if self.is_bound else self.initial.get(self.orders_parent_field)
        inherited = choices.get(str(parent_id), baseline)
        self.post_orders_defaults = {"choices": choices, "fallback": baseline,
                                    "parent": self.orders_parent_field, "field": self.orders_field,
                                    "own": bool(getattr(self.instance, self.orders_field).strip())}
        if not self.is_bound and not getattr(self.instance, self.orders_field).strip():
            self.initial[self.orders_field] = inherited["text"]
        if not self.is_bound:
            self.initial["post_orders_inheritance"] = inherited["signature"]

    def clean(self):
        data = super().clean() or {}
        if not hasattr(self, "orders_organization"):
            return data
        parent = data.get(self.orders_parent_field) if self.orders_parent_field else None
        inherited = parent.effective_post_orders if parent else self.orders_organization.default_post_orders
        text = data.get(self.orders_field, "")
        own = getattr(self.instance, self.orders_field)
        original = inherited
        signature = data.get("post_orders_inheritance")
        if signature:
            try:
                shown = signing.loads(signature, salt="post-orders-inheritance")
            except signing.BadSignature:
                self.add_error("post_orders_inheritance", "The inherited orders could not be verified. Reload this page.")
            else:
                # A default changed while this form was open: unchanged prefilled text must not
                # accidentally become an override of the newer default.
                if (shown["organization"] == str(self.orders_organization.pk)
                        and shown["field"] == self.orders_field
                        and shown["parent"] == (str(parent.pk) if parent else "")):
                    original = shown["text"]
        if not text.strip() or (not own.strip() and text in (inherited, original)):
            data[self.orders_field] = ""
        return data
