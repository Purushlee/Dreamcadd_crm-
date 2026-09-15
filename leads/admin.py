from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (
    Branch, CallLog, CompanySettings, Course, CourseCategory, Followup,
    ImportBatch, Lead, LeadActivity, LeadAssignment, LeadSource, Notification,
    SecurityAuditLog, TelecallerProfile, User, WhatsAppMessage, WhatsAppTemplate
)


@admin.register(User)
class DreamCaddUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("DreamCadd", {"fields": ("role", "phone", "status")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("DreamCadd", {"fields": ("role", "phone", "status")}),
    )
    list_display = ("username", "get_full_name", "role", "status", "is_staff")
    list_filter = ("role", "status")


@admin.register(TelecallerProfile)
class TelecallerProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "max_leads", "daily_call_target", "specialization")


@admin.register(CompanySettings)
class CompanySettingsAdmin(admin.ModelAdmin):
    list_display = ("company_name", "main_phone", "email", "website")


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("branch_name", "location", "phone", "status")
    list_filter = ("status",)


@admin.register(LeadSource)
class LeadSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "active")


@admin.register(CourseCategory)
class CourseCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "description")


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("course_name", "category", "duration", "fee", "status")
    list_filter = ("status", "category")
    search_fields = ("course_name",)


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = ("batch_code", "file_name", "uploaded_by", "total_rows", "new_leads", "uploaded_at")
    list_filter = ("uploaded_at",)


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "priority", "status", "assigned_to", "interested_course", "created_at")
    list_filter = ("status", "priority", "assigned_to", "interested_course")
    search_fields = ("name", "phone", "email", "college")


@admin.register(LeadAssignment)
class LeadAssignmentAdmin(admin.ModelAdmin):
    list_display = ("lead", "caller", "assigned_date", "status")
    list_filter = ("status", "caller")


@admin.register(Followup)
class FollowupAdmin(admin.ModelAdmin):
    list_display = ("lead", "caller", "scheduled_date", "status")
    list_filter = ("status", "caller")


@admin.register(CallLog)
class CallLogAdmin(admin.ModelAdmin):
    list_display = ("lead", "caller", "call_result", "duration_seconds", "created_at")
    list_filter = ("call_result", "caller")


@admin.register(LeadActivity)
class LeadActivityAdmin(admin.ModelAdmin):
    list_display = ("lead", "activity_type", "actor", "timestamp")
    list_filter = ("activity_type",)


@admin.register(WhatsAppTemplate)
class WhatsAppTemplateAdmin(admin.ModelAdmin):
    list_display = ("template_name", "meta_template_name", "language", "category", "status")
    list_filter = ("status", "category")


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(admin.ModelAdmin):
    list_display = ("lead", "direction", "status", "timestamp")
    list_filter = ("direction", "status")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "is_read", "created_at")
    list_filter = ("is_read",)


@admin.register(SecurityAuditLog)
class SecurityAuditLogAdmin(admin.ModelAdmin):
    list_display = ("user", "action", "ip_address", "timestamp")
    list_filter = ("action", "timestamp")
    search_fields = ("user__username", "ip_address", "details")

