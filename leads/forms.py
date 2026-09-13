from django import forms
from django.contrib.auth.forms import AuthenticationForm

from .models import (
    Branch, CallLog, CompanySettings, Course, Followup, Lead, TelecallerProfile,
    User, WhatsAppTemplate
)


class StyledAuthenticationForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"class": "form-control", "placeholder": "Username"})
        self.fields["password"].widget.attrs.update({"class": "form-control", "placeholder": "Password"})


class ExcelUploadForm(forms.Form):
    file = forms.FileField(
        widget=forms.FileInput(attrs={"class": "form-control"}),
        help_text="Excel file (.xlsx, .xls, .xlsm) with columns: Name, Phone, Education, College, Course, Email"
    )

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith((".xlsx", ".xls", ".xlsm")):
            raise forms.ValidationError("Please upload a valid .xlsx/.xls/.xlsm file.")
        return f


class TelecallerCreateForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput(attrs={"class": "form-control"}))
    first_name = forms.CharField(widget=forms.TextInput(attrs={"class": "form-control"}))
    last_name = forms.CharField(widget=forms.TextInput(attrs={"class": "form-control"}))
    max_leads = forms.IntegerField(initial=100, widget=forms.NumberInput(attrs={"class": "form-control"}))
    daily_call_target = forms.IntegerField(initial=30, widget=forms.NumberInput(attrs={"class": "form-control"}))

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "phone"]
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
        }


class CompanySettingsForm(forms.ModelForm):
    class Meta:
        model = CompanySettings
        fields = ["company_name", "website", "main_phone", "email", "address"]
        widgets = {
            "company_name": forms.TextInput(attrs={"class": "form-control"}),
            "website": forms.URLInput(attrs={"class": "form-control"}),
            "main_phone": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "address": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }


class BranchForm(forms.ModelForm):
    class Meta:
        model = Branch
        fields = ["branch_name", "location", "address", "phone", "status"]
        widgets = {
            "branch_name": forms.TextInput(attrs={"class": "form-control"}),
            "location": forms.TextInput(attrs={"class": "form-control"}),
            "address": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "status": forms.Select(attrs={"class": "form-select"}),
        }


class CourseForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ["course_name", "category", "duration", "fee", "description", "eligibility", "syllabus_file", "status"]
        widgets = {
            "course_name": forms.TextInput(attrs={"class": "form-control"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "duration": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. 3 Months"}),
            "fee": forms.NumberInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "eligibility": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. BE / B.Tech Civil, Mechanical"}),
            "syllabus_file": forms.FileInput(attrs={"class": "form-control"}),
            "status": forms.Select(attrs={"class": "form-select"}),
        }


class WhatsAppTemplateForm(forms.ModelForm):
    class Meta:
        model = WhatsAppTemplate
        fields = ["template_name", "meta_template_name", "language", "category", "body_text", "status"]
        widgets = {
            "template_name": forms.TextInput(attrs={"class": "form-control"}),
            "meta_template_name": forms.TextInput(attrs={"class": "form-control"}),
            "language": forms.TextInput(attrs={"class": "form-control"}),
            "category": forms.TextInput(attrs={"class": "form-control"}),
            "body_text": forms.Textarea(attrs={"class": "form-control", "rows": 4, "placeholder": "Use variables like {{name}}, {{course}}, {{caller_name}}, {{company_name}}, {{fee}}"}),
            "status": forms.Select(attrs={"class": "form-select"}),
        }


class LeadForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = ["name", "phone", "email", "education", "college", "interested_course", "lead_source", "priority", "status"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "education": forms.TextInput(attrs={"class": "form-control"}),
            "college": forms.TextInput(attrs={"class": "form-control"}),
            "interested_course": forms.Select(attrs={"class": "form-select"}),
            "lead_source": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "status": forms.Select(attrs={"class": "form-select"}),
        }


class FollowupForm(forms.ModelForm):
    scheduled_date = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}),
        required=True
    )

    class Meta:
        model = Followup
        fields = ["scheduled_date", "status", "remarks"]
        widgets = {
            "status": forms.Select(attrs={"class": "form-select"}),
            "remarks": forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Follow-up remarks..."}),
        }


class CallLogForm(forms.ModelForm):
    class Meta:
        model = CallLog
        fields = ["call_result", "duration_seconds", "remarks"]
        widgets = {
            "call_result": forms.Select(attrs={"class": "form-select"}),
            "duration_seconds": forms.NumberInput(attrs={"class": "form-control", "placeholder": "Duration in seconds"}),
            "remarks": forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Call notes / discussion details..."}),
        }
