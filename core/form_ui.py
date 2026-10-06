from django import forms
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
