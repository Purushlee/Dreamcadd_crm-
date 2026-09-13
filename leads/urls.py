from django.contrib.auth.views import LogoutView
from django.urls import path

from . import n8n_views, views

urlpatterns = [
    path("", views.home_redirect, name="home"),
    path("login/", views.RoleAwareLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(next_page="login"), name="logout"),

    # MD Control Views
    path("md/", views.md_dashboard, name="md_dashboard"),
    path("md/upload-excel/", views.upload_excel, name="upload_excel"),
    path("md/callers/", views.manage_callers, name="manage_callers"),
    path("md/bulk-allocate/", views.bulk_allocate, name="bulk_allocate"),
    path("md/company-settings/", views.company_settings_view, name="company_settings"),
    path("md/courses/", views.manage_courses, name="manage_courses"),
    path("md/templates/", views.whatsapp_templates_view, name="whatsapp_templates"),
    path("md/import-history/", views.import_history_view, name="import_history"),
    path("md/allocation-history/", views.allocation_history_view, name="allocation_history"),
    path("md/allocation-history/export/<int:batch_id>/", views.export_allocation_report, name="export_allocation_report"),
    path("md/callers/<int:caller_id>/report/", views.telecaller_report_view, name="telecaller_report"),
    path("md/callers/<int:caller_id>/export/", views.export_telecaller_csv, name="export_telecaller_csv"),
    path("md/unassign-leads/", views.unassign_leads, name="unassign_leads"),
    path("md/export-leads/", views.export_leads_csv, name="export_leads_csv"),
    path("md/export-history/", views.export_history_view, name="export_history"),
    path("md/export-call-history/", views.export_call_history_csv, name="export_call_history_csv"),
    path("md/export-followups/", views.export_followup_report_csv, name="export_followup_report_csv"),
    path("md/clear-database/", views.clear_database, name="clear_database"),

    # Telecaller & Lead Detail Views
    path("telecaller/", views.telecaller_dashboard, name="telecaller_dashboard"),
    path("leads/<int:lead_id>/", views.lead_detail, name="lead_detail"),
    path("api/leads/<int:lead_id>/quick-call-log/", views.api_quick_call_log, name="api_quick_call_log"),
    path("api/leads/<int:lead_id>/edit-response/", views.api_edit_response, name="api_edit_response"),
    path("api/session-check/", views.api_session_check, name="api_session_check"),

    # Meta WhatsApp Webhook Direct
    path("webhook/whatsapp/", views.whatsapp_webhook, name="whatsapp_webhook"),

    # n8n Integration API Routes
    path("api/n8n/pending-followups/", n8n_views.get_pending_followups, name="n8n_pending_followups"),
    path("api/n8n/incoming-whatsapp/", n8n_views.handle_n8n_incoming_whatsapp, name="n8n_incoming_whatsapp"),
    path("api/n8n/course-lookup/", n8n_views.get_course_lookup, name="n8n_course_lookup"),
    path("api/n8n/company-info/", n8n_views.get_company_info, name="n8n_company_info"),
    path("api/n8n/trigger-hot-alert/", n8n_views.trigger_hot_alert, name="n8n_trigger_hot_alert"),
    # Workflow 2 — Student Reply Intent & Hot Lead
    path("api/n8n/find-lead/", n8n_views.find_lead, name="n8n_find_lead"),
    path("api/n8n/update-lead/", n8n_views.update_lead_status, name="n8n_update_lead"),
]
