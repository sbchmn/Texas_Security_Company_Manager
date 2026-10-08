from django import forms

from .form_ui import WorkflowForm, WorkflowModelForm
from .models import LeavePolicy


class LeavePolicyForm(WorkflowModelForm):
    class Meta:
        model = LeavePolicy
        fields = ["enabled", "annual_hours", "grant_method", "daily_hours", "carryover_cap", "fallback_pay_rate"]
        labels = {"enabled": "Enable paid leave bank", "annual_hours": "Annual allowance (hours)",
                  "daily_hours": "Default hours per calendar day", "fallback_pay_rate": "Fallback hourly leave rate (USD)"}
        help_texts = {
            "annual_hours": "Automatic grants are prorated at enrollment. Accrual earns eligible days after completed periods.",
            "daily_hours": "Suggestion only when no published shifts are displaced. A reviewer must confirm the total.",
            "fallback_pay_rate": "Used only when no displaced shift rate and no employee hourly rate are available.",
        }


class LeaveEnrollmentForm(WorkflowForm):
    eligible_from = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))


class LeaveAdjustmentForm(WorkflowForm):
    hours = forms.DecimalField(max_digits=9, decimal_places=2, help_text="Positive grants hours; negative removes available hours.")
    reason = forms.CharField(min_length=5, max_length=255)


class LeaveApprovalForm(WorkflowForm):
    confirmed_hours = forms.DecimalField(max_digits=9, decimal_places=2, min_value=0.01,
                                         label="Confirmed paid leave hours")
    confirm = forms.BooleanField(label="I confirm these hours and the bank reservation")

