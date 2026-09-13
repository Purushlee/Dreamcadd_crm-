import csv
import re
import json
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.views import LoginView
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .api_security import verify_meta_webhook_signature
from .forms import (
    BranchForm, CallLogForm, CompanySettingsForm, CourseForm, ExcelUploadForm,
    FollowupForm, LeadForm, StyledAuthenticationForm, TelecallerCreateForm,
    WhatsAppTemplateForm
)
from .models import (
    AllocationBatch, Branch, CallLog, CompanySettings, Course, ExportHistory, Followup, ImportBatch, Lead,
    LeadActivity, LeadAssignment, LeadSource, Notification, TelecallerProfile,
    User, UserSessionLog, WhatsAppMessage, WhatsAppTemplate
)
from .services import (
    assign_lead_round_robin, import_leads_from_excel, log_lead_activity,
    process_incoming_whatsapp, render_whatsapp_template, send_new_lead_message,
    send_whatsapp_text
)


import time
from django.core.cache import cache
from django.db.models.signals import post_save, post_delete

_LAST_SYSTEM_CHANGE = time.time()


def mark_system_changed(*args, **kwargs):
    global _LAST_SYSTEM_CHANGE
    now_ts = time.time()
    _LAST_SYSTEM_CHANGE = now_ts
    try:
        cache.set("system_last_changed", now_ts, timeout=86400 * 30)
    except Exception:
        pass
    return now_ts


def get_system_last_changed():
    try:
        val = cache.get("system_last_changed")
        if val is not None:
            return float(val)
    except Exception:
        pass
    global _LAST_SYSTEM_CHANGE
    return _LAST_SYSTEM_CHANGE


# Auto-connect signals to update system_last_changed on ANY database modification
for model_cls in [Lead, CallLog, LeadAssignment, User, Followup, ImportBatch, AllocationBatch]:
    post_save.connect(mark_system_changed, sender=model_cls, dispatch_uid=f"mark_system_changed_save_{model_cls.__name__}")
    post_delete.connect(mark_system_changed, sender=model_cls, dispatch_uid=f"mark_system_changed_del_{model_cls.__name__}")


def is_md(user):
    return user.is_authenticated and user.is_md


def is_telecaller(user):
    return user.is_authenticated and (user.role == User.Role.TELECALLER or user.is_md or user.is_superuser)


class RoleAwareLoginView(LoginView):
    template_name = "leads/login.html"
    authentication_form = StyledAuthenticationForm

    def get_success_url(self):
        user = self.request.user
        if user.is_md:
            return reverse_lazy("md_dashboard")
        return reverse_lazy("telecaller_dashboard")


@login_required
def home_redirect(request):
    if request.user.is_md:
        return redirect("md_dashboard")
    return redirect("telecaller_dashboard")


# ---------------------------------------------------------------------------
# MD Dashboards & Control Views
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# MD Dashboards & Control Views
# ---------------------------------------------------------------------------

@user_passes_test(is_md, login_url="login")
def md_dashboard(request):
    leads = Lead.objects.all()
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    total_leads = leads.count()
    unallocated_count = leads.filter(assigned_to__isnull=True).count()
    allocated_count = leads.filter(assigned_to__isnull=False).count()
    converted_leads = leads.filter(status=Lead.Status.CONVERTED).count()
    conversion_rate = round((converted_leads / total_leads * 100), 1) if total_leads > 0 else 0.0

    stats = {
        "total": total_leads,
        "unallocated": unallocated_count,
        "allocated": allocated_count,
        "new": leads.filter(status=Lead.Status.NEW).count(),
        "assigned": leads.filter(status=Lead.Status.ASSIGNED).count(),
        "contacted": leads.filter(status=Lead.Status.CONTACTED).count(),
        "interested": leads.filter(status=Lead.Status.INTERESTED).count(),
        "converted": converted_leads,
        "hot_leads": leads.filter(priority=Lead.Priority.HOT).count(),
        "conversion_rate": conversion_rate,
        "today_calls": CallLog.objects.filter(created_at__gte=today_start).count(),
        "overdue_followups": Followup.objects.filter(status=Followup.Status.PENDING, scheduled_date__lt=now).count(),
    }

    # Telecallers with presence & session monitoring
    telecallers_qs = User.objects.filter(role=User.Role.TELECALLER).select_related("profile").annotate(
        assigned_count=Count("assigned_leads"),
        unique_contacted_count=Count("assigned_leads", filter=Q(assigned_leads__contacted=True), distinct=True),
        converted_count=Count("assigned_leads", filter=Q(assigned_leads__status=Lead.Status.CONVERTED)),
        calls_count=Count("call_logs"),
        today_calls=Count("call_logs", filter=Q(call_logs__created_at__gte=today_start))
    )

    telecaller_list = []
    calls_chart_labels = []
    calls_chart_data = []
    conversions_chart_data = []

    for tc in telecallers_qs:
        session = UserSessionLog.objects.filter(user=tc, login_time__gte=today_start).order_by("-login_time").first()
        working_time_str = "—"
        if session:
            end_t = session.logout_time or session.last_activity or now
            diff_secs = (end_t - session.login_time).total_seconds()
            hrs = int(diff_secs // 3600)
            mins = int((diff_secs % 3600) // 60)
            working_time_str = f"{hrs}h {mins}m"

        telecaller_list.append({
            "id": tc.id,
            "username": tc.username,
            "full_name": tc.get_full_name() or tc.username,
            "status": tc.status,
            "presence_status": tc.presence_status,
            "presence_color": tc.presence_color,
            "login_time": session.login_time if session else None,
            "last_activity": tc.last_activity,
            "working_time": working_time_str,
            "assigned_count": tc.assigned_count,
            "unique_contacted_count": tc.unique_contacted_count,
            "total_call_attempts": tc.calls_count,
            "converted_count": tc.converted_count,
            "calls_count": tc.calls_count,
            "today_calls": tc.today_calls,
            "max_leads": tc.profile.max_leads if hasattr(tc, "profile") else 100,
            "daily_target": tc.profile.daily_call_target if hasattr(tc, "profile") else 30,
        })
        calls_chart_labels.append(tc.get_full_name() or tc.username)
        calls_chart_data.append(tc.today_calls)
        conversions_chart_data.append(tc.converted_count)

    # Call Results Distribution Chart
    results_qs = CallLog.objects.values("call_result").annotate(count=Count("id"))
    results_dict = {res["call_result"]: res["count"] for res in results_qs}
    call_results_data = [
        results_dict.get(CallLog.Result.CONNECTED, 0),
        results_dict.get(CallLog.Result.INTERESTED, 0),
        results_dict.get(CallLog.Result.CALLBACK_REQUESTED, 0),
        results_dict.get(CallLog.Result.NOT_CONNECTED, 0),
        results_dict.get(CallLog.Result.BUSY, 0),
        results_dict.get(CallLog.Result.NOT_INTERESTED, 0),
        results_dict.get(CallLog.Result.CONVERTED, 0),
    ]

    upload_form = ExcelUploadForm()
    recent_activities = LeadActivity.objects.select_related("lead", "actor").all()[:15]

    return render(request, "leads/md_dashboard.html", {
        "stats": stats,
        "telecallers": telecaller_list,
        "upload_form": upload_form,
        "recent_leads": leads.select_related("assigned_to", "interested_course").order_by("-created_at")[:20],
        "recent_activities": recent_activities,
        "chart_calls_labels": json.dumps(calls_chart_labels),
        "chart_calls_data": json.dumps(calls_chart_data),
        "chart_conversions_data": json.dumps(conversions_chart_data),
        "chart_results_data": json.dumps(call_results_data),
    })


@user_passes_test(is_md, login_url="login")
def upload_excel(request):
    if request.method == "POST":
        form = ExcelUploadForm(request.POST, request.FILES)
        if form.is_valid():
            excel_file = form.cleaned_data["file"]
            batch_label = f"{excel_file.name} ({request.user.username})"
            summary = import_leads_from_excel(excel_file, batch_label, uploaded_by=request.user)

            messages.success(
                request,
                f"Import Done — New: {summary['new']}, Updated: {summary['updated']}, "
                f"Duplicates: {summary['duplicate_ignored']}. All new leads are ready in the Unallocated Pool."
            )
            for err in summary["errors"][:5]:
                messages.warning(request, err)
            return redirect("bulk_allocate")
        else:
            messages.error(request, "Invalid file format uploaded.")
    return redirect("bulk_allocate")


@user_passes_test(is_md, login_url="login")
def manage_callers(request):
    telecallers = User.objects.filter(role=User.Role.TELECALLER).select_related("profile").annotate(
        assigned_count=Count("assigned_leads"),
        worked_count=Count("assigned_leads", filter=~Q(assigned_leads__status__in=[Lead.Status.NEW, Lead.Status.ASSIGNED])),
        pending_count=Count("assigned_leads", filter=Q(assigned_leads__status__in=[Lead.Status.NEW, Lead.Status.ASSIGNED])),
        calls_count=Count("call_logs"),
        interested_count=Count("assigned_leads", filter=Q(assigned_leads__status=Lead.Status.INTERESTED)),
        followup_count=Count("assigned_leads", filter=Q(assigned_leads__status=Lead.Status.CALL_BACK)),
        converted_count=Count("assigned_leads", filter=Q(assigned_leads__status=Lead.Status.CONVERTED))
    ).order_by("username")

    if request.method == "POST":
        if "create_caller" in request.POST:
            form = TelecallerCreateForm(request.POST)
            if form.is_valid():
                user = form.save(commit=False)
                user.role = User.Role.TELECALLER
                user.set_password(form.cleaned_data["password"])
                user.save()

                profile = user.telecaller_profile
                profile.max_leads = form.cleaned_data["max_leads"]
                profile.daily_call_target = form.cleaned_data["daily_call_target"]
                profile.save()

                messages.success(request, f"Telecaller '{user.username}' created successfully.")
                return redirect("manage_callers")
        elif "toggle_status" in request.POST:
            caller_id = request.POST.get("caller_id")
            caller = get_object_or_404(User, id=caller_id, role=User.Role.TELECALLER)
            caller.status = "INACTIVE" if caller.status == "ACTIVE" else "ACTIVE"
            caller.save(update_fields=["status"])
            messages.info(request, f"Status for '{caller.username}' set to {caller.status}.")
            return redirect("manage_callers")
        elif "delete_caller" in request.POST:
            caller_id = request.POST.get("caller_id")
            caller = get_object_or_404(User, id=caller_id, role=User.Role.TELECALLER)
            username = caller.username
            # Unassign leads assigned to this caller so they return to the pool
            reassigned = Lead.objects.filter(assigned_to=caller).update(assigned_to=None, status=Lead.Status.NEW)
            caller.delete()
            messages.success(request, f"Telecaller '{username}' deleted successfully. {reassigned} assigned lead(s) reset to unassigned pool.")
            return redirect("manage_callers")
        elif "update_caller_limits" in request.POST:
            caller_id = request.POST.get("caller_id")
            caller = get_object_or_404(User, id=caller_id, role=User.Role.TELECALLER)
            profile = caller.telecaller_profile
            try:
                profile.max_leads = int(request.POST.get("max_leads", profile.max_leads))
                profile.daily_call_target = int(request.POST.get("daily_call_target", profile.daily_call_target))
                profile.save()
                messages.success(request, f"Limits updated for '{caller.username}': Max Capacity={profile.max_leads}, Daily Target={profile.daily_call_target}.")
            except ValueError:
                messages.error(request, "Invalid number provided for limits.")
            return redirect("manage_callers")

    form = TelecallerCreateForm()
    return render(request, "leads/manage_callers.html", {"telecallers": telecallers, "form": form})


@user_passes_test(is_md, login_url="login")
def bulk_allocate(request):
    telecallers = User.objects.filter(role=User.Role.TELECALLER, status="ACTIVE").select_related("profile")
    courses = Course.objects.all()
    sources = LeadSource.objects.all()

    # STRICT UNALLOCATED POOL FILTER: Only unallocated leads appear in Allocation Workbench!
    leads = Lead.objects.select_related("assigned_to", "interested_course", "lead_source").filter(assigned_to__isnull=True)

    # Filtering
    status_filter = request.GET.get("status", "")
    course_filter = request.GET.get("course", "")
    source_filter = request.GET.get("source", "")

    if status_filter:
        leads = leads.filter(status=status_filter)
    if course_filter:
        leads = leads.filter(interested_course_id=course_filter)
    if source_filter:
        leads = leads.filter(lead_source_id=source_filter)

    if request.method == "POST":
        if "auto_allocate_batch" in request.POST:
            target_caller_id = request.POST.get("target_caller_id")
            batch_count_str = request.POST.get("batch_count", "100")
            try:
                batch_count = int(batch_count_str)
            except ValueError:
                batch_count = 100

            if target_caller_id:
                target_caller = get_object_or_404(User, id=target_caller_id, role=User.Role.TELECALLER)
                unassigned_qs = Lead.objects.filter(assigned_to__isnull=True)
                if course_filter:
                    unassigned_qs = unassigned_qs.filter(interested_course_id=course_filter)

                leads_to_assign = list(unassigned_qs[:batch_count])
                if leads_to_assign:
                    batch_code = f"ALLOC-{timezone.now().strftime('%Y%m%d%H%M%S')}-{target_caller.username[:10].upper()}"
                    alloc_batch = AllocationBatch.objects.create(
                        batch_code=batch_code,
                        caller=target_caller,
                        allocated_by=request.user,
                        lead_count=len(leads_to_assign),
                        notes=f"Auto batch allocation of {len(leads_to_assign)} leads"
                    )

                    assigned_count = 0
                    for lead in leads_to_assign:
                        lead.assigned_to = target_caller
                        lead.allocation_batch = alloc_batch
                        lead.assigned_at = timezone.now()
                        if lead.status == Lead.Status.NEW:
                            lead.status = Lead.Status.ASSIGNED
                        lead.save(update_fields=["assigned_to", "allocation_batch", "assigned_at", "status", "updated_at"])
                        LeadAssignment.objects.create(lead=lead, caller=target_caller)
                        log_lead_activity(lead, "AUTO_ASSIGNED", f"Batch assigned to {target_caller.username} (Batch {batch_code})", actor=request.user)
                        assigned_count += 1

                    messages.success(
                        request,
                        f"Auto-assigned {assigned_count} unassigned lead(s) to '{target_caller.get_full_name() or target_caller.username}' (Batch: {batch_code})."
                    )
                else:
                    messages.warning(request, "No unassigned leads found matching criteria.")
                return redirect("bulk_allocate")

        elif "manual_allocate" in request.POST or "lead_ids" in request.POST:
            selected_lead_ids = request.POST.getlist("lead_ids")
            target_caller_id = request.POST.get("target_caller_id")

            if selected_lead_ids and target_caller_id:
                target_caller = get_object_or_404(User, id=target_caller_id, role=User.Role.TELECALLER)
                leads_to_assign = Lead.objects.filter(id__in=selected_lead_ids)
                if leads_to_assign.exists():
                    batch_code = f"ALLOC-MANUAL-{timezone.now().strftime('%Y%m%d%H%M%S')}-{target_caller.username[:10].upper()}"
                    alloc_batch = AllocationBatch.objects.create(
                        batch_code=batch_code,
                        caller=target_caller,
                        allocated_by=request.user,
                        lead_count=leads_to_assign.count(),
                        notes="Manual selection allocation"
                    )

                    updated_count = 0
                    for lead in leads_to_assign:
                        lead.assigned_to = target_caller
                        lead.allocation_batch = alloc_batch
                        lead.assigned_at = timezone.now()
                        if lead.status == Lead.Status.NEW:
                            lead.status = Lead.Status.ASSIGNED
                        lead.save(update_fields=["assigned_to", "allocation_batch", "assigned_at", "status", "updated_at"])
                        LeadAssignment.objects.create(lead=lead, caller=target_caller)
                        log_lead_activity(lead, "REASSIGNED", f"Manually assigned to {target_caller.username} (Batch {batch_code})", actor=request.user)
                        updated_count += 1

                    messages.success(request, f"Successfully assigned {updated_count} lead(s) to {target_caller.username} (Batch: {batch_code}).")
                return redirect("bulk_allocate")

    unassigned_total = Lead.objects.filter(assigned_to__isnull=True).count()

    return render(request, "leads/bulk_allocate.html", {
        "leads": leads.order_by("-created_at")[:500],
        "telecallers": telecallers,
        "courses": courses,
        "sources": sources,
        "status_choices": Lead.Status.choices,
        "selected_status": status_filter,
        "selected_course": course_filter,
        "selected_source": source_filter,
        "unassigned_total": unassigned_total,
    })


@user_passes_test(is_md, login_url="login")
def allocation_history_view(request):
    batches = AllocationBatch.objects.select_related("caller", "allocated_by", "import_batch").order_by("-created_at")
    return render(request, "leads/allocation_history.html", {"batches": batches})


@user_passes_test(is_md, login_url="login")
def export_allocation_report(request, batch_id):
    batch = get_object_or_404(AllocationBatch, id=batch_id)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="Allocation_Report_{batch.batch_code}.csv"'

    writer = csv.writer(response)
    writer.writerow(["Allocation Batch Code", batch.batch_code])
    writer.writerow(["Allocated Date", batch.created_at.strftime("%Y-%m-%d %H:%M:%S")])
    writer.writerow(["Allocated By", batch.allocated_by.username if batch.allocated_by else "System"])
    writer.writerow(["Telecaller", batch.caller.get_full_name() or batch.caller.username])
    writer.writerow(["Total Allocated Count", batch.lead_count])
    writer.writerow([])
    writer.writerow(["Lead ID", "Lead Name", "Phone", "Email", "Course", "Current Status", "Priority", "Assigned Date"])

    leads = Lead.objects.filter(allocation_batch=batch).select_related("interested_course")
    for l in leads:
        writer.writerow([
            l.id, l.name, l.phone, l.email or "",
            l.interested_course.course_name if l.interested_course else "—",
            l.status, l.priority, l.assigned_at.strftime("%Y-%m-%d %H:%M") if l.assigned_at else ""
        ])

    return response


@user_passes_test(is_md, login_url="login")
def telecaller_report_view(request, caller_id):
    from datetime import timedelta
    caller = get_object_or_404(User, id=caller_id, role=User.Role.TELECALLER)
    leads = Lead.objects.filter(assigned_to=caller)

    date_filter = request.GET.get("date_range", "all")
    now = timezone.now()
    today = now.date()

    call_logs = CallLog.objects.filter(caller=caller).select_related("lead").order_by("-call_time")

    if date_filter == "today":
        call_logs = call_logs.filter(call_time__date=today)
    elif date_filter == "yesterday":
        call_logs = call_logs.filter(call_time__date=today - timedelta(days=1))
    elif date_filter == "this_week":
        call_logs = call_logs.filter(call_time__gte=now - timedelta(days=7))
    elif date_filter == "this_month":
        call_logs = call_logs.filter(call_time__gte=now - timedelta(days=30))

    total_assigned = leads.count()
    completed_count = leads.exclude(status__in=[Lead.Status.NEW, Lead.Status.ASSIGNED]).count()
    pending_count = leads.filter(status__in=[Lead.Status.NEW, Lead.Status.ASSIGNED]).count()

    calls_made = call_logs.count()
    connected_calls = call_logs.filter(call_result=CallLog.Result.CONNECTED).count()
    not_connected_calls = call_logs.filter(call_result=CallLog.Result.NOT_CONNECTED).count()
    busy_calls = call_logs.filter(call_result=CallLog.Result.BUSY).count()
    switched_off_calls = call_logs.filter(call_result=CallLog.Result.SWITCHED_OFF).count()
    wrong_number_calls = call_logs.filter(call_result=CallLog.Result.WRONG_NUMBER).count()

    interested_count = leads.filter(status=Lead.Status.INTERESTED).count()
    not_interested_count = leads.filter(status=Lead.Status.NOT_INTERESTED).count()
    call_back_count = leads.filter(status=Lead.Status.CALL_BACK).count()
    converted_count = leads.filter(status=Lead.Status.CONVERTED).count()

    followups = Followup.objects.filter(caller=caller)
    pending_followups = followups.filter(status=Followup.Status.PENDING).count()
    completed_followups = followups.filter(status=Followup.Status.DONE).count()
    overdue_followups = followups.filter(status=Followup.Status.PENDING, scheduled_date__lt=now).count()

    session_logs = UserSessionLog.objects.filter(user=caller).order_by("-login_time")[:15]
    latest_session = session_logs.first()

    metrics = {
        "total_assigned": total_assigned,
        "completed_count": completed_count,
        "pending_count": pending_count,
        "calls_made": calls_made,
        "connected_calls": connected_calls,
        "not_connected_calls": not_connected_calls,
        "busy_calls": busy_calls,
        "switched_off_calls": switched_off_calls,
        "wrong_number_calls": wrong_number_calls,
        "interested_count": interested_count,
        "not_interested_count": not_interested_count,
        "call_back_count": call_back_count,
        "converted_count": converted_count,
        "pending_followups": pending_followups,
        "completed_followups": completed_followups,
        "overdue_followups": overdue_followups,
    }

    return render(request, "leads/telecaller_report.html", {
        "caller": caller,
        "metrics": metrics,
        "session_logs": session_logs,
        "latest_session": latest_session,
        "call_logs": call_logs[:50],
        "assigned_leads": leads.select_related("interested_course", "preferred_branch").order_by("-updated_at"),
        "date_filter": date_filter,
    })


@user_passes_test(is_md, login_url="login")
@require_POST
def unassign_leads(request):
    """
    Unassign leads from a telecaller (or batch) without deleting master records.
    Resets assigned_to=None and status=NEW, returning leads to the unassigned pool.
    """
    lead_ids = request.POST.getlist("lead_ids")
    batch_id = request.POST.get("batch_id")
    caller_id = request.POST.get("caller_id")

    if lead_ids:
        qs = Lead.objects.filter(id__in=lead_ids)
        count = qs.count()
        for lead in qs:
            log_lead_activity(lead, "UNASSIGNED", f"Unassigned from {lead.assigned_to.username if lead.assigned_to else 'telecaller'}", actor=request.user)
            lead.assigned_to = None
            lead.allocation_batch = None
            if lead.status == Lead.Status.ASSIGNED:
                lead.status = Lead.Status.NEW
            lead.save(update_fields=["assigned_to", "allocation_batch", "status", "updated_at"])
        messages.success(request, f"Successfully unassigned {count} lead(s). They are now back in the unallocated pool.")

    elif batch_id:
        batch = get_object_or_404(AllocationBatch, id=batch_id)
        qs = Lead.objects.filter(allocation_batch=batch)
        count = qs.count()
        for lead in qs:
            log_lead_activity(lead, "UNASSIGNED", f"Unassigned batch {batch.batch_code}", actor=request.user)
            lead.assigned_to = None
            lead.allocation_batch = None
            if lead.status == Lead.Status.ASSIGNED:
                lead.status = Lead.Status.NEW
            lead.save(update_fields=["assigned_to", "allocation_batch", "status", "updated_at"])
        batch.delete()
        messages.success(request, f"Successfully unassigned batch {batch.batch_code} ({count} leads).")

    elif caller_id:
        caller = get_object_or_404(User, id=caller_id, role=User.Role.TELECALLER)
        qs = Lead.objects.filter(assigned_to=caller)
        count = qs.count()
        for lead in qs:
            log_lead_activity(lead, "UNASSIGNED", f"Unassigned from telecaller {caller.username}", actor=request.user)
            lead.assigned_to = None
            lead.allocation_batch = None
            if lead.status == Lead.Status.ASSIGNED:
                lead.status = Lead.Status.NEW
            lead.save(update_fields=["assigned_to", "allocation_batch", "status", "updated_at"])
        messages.success(request, f"Successfully unassigned all {count} lead(s) from {caller.username}.")

    referer = request.META.get("HTTP_REFERER")
    if referer:
        return redirect(referer)
    return redirect("bulk_allocate")


@user_passes_test(is_md, login_url="login")
def export_telecaller_csv(request, caller_id):
    """Export complete dataset and work log for a specific telecaller."""
    caller = get_object_or_404(User, id=caller_id, role=User.Role.TELECALLER)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="Telecaller_{caller.username}_Work_{timezone.now().strftime("%Y%m%d")}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        "Lead ID", "Customer Name", "Phone", "Course", "Import Batch",
        "Assigned Date", "Telecaller", "Lead Status", "Call Count",
        "Last Call Date", "Last Call Result", "Last Call Notes",
        "Followup Date", "Followup Time", "Conversion Status", "Last Activity"
    ])

    leads = Lead.objects.filter(assigned_to=caller).select_related("interested_course", "import_batch").order_by("-updated_at")

    for l in leads:
        last_call = l.call_logs.order_by("-call_time").first()
        last_followup = l.followups.order_by("-scheduled_date").first()
        writer.writerow([
            l.id,
            l.name,
            l.phone,
            l.interested_course.course_name if l.interested_course else "",
            l.import_batch.batch_code if l.import_batch else (l.source_batch or ""),
            l.assigned_at.strftime("%Y-%m-%d %H:%M") if l.assigned_at else "",
            caller.get_full_name() or caller.username,
            l.get_status_display(),
            l.call_logs.count(),
            last_call.call_time.strftime("%Y-%m-%d %H:%M") if last_call else "",
            last_call.get_call_result_display() if last_call else "",
            last_call.remarks if last_call else "",
            last_followup.scheduled_date.strftime("%Y-%m-%d") if last_followup else "",
            last_followup.scheduled_date.strftime("%H:%M") if last_followup else "",
            "Converted" if l.status == Lead.Status.CONVERTED else "In Progress",
            l.updated_at.strftime("%Y-%m-%d %H:%M")
        ])

    ExportHistory.objects.create(
        user=request.user,
        export_type=f"Telecaller Work Export ({caller.username})",
        filters_applied={"caller": caller.username},
        record_count=leads.count()
    )

    return response


@user_passes_test(is_md, login_url="login")
def export_call_history_csv(request):
    """Export full call attempt history log CSV."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="DreamCadd_CallHistory_{timezone.now().strftime("%Y%m%d_%H%M")}.csv"'

    writer = csv.writer(response)
    writer.writerow(["Call Log ID", "Call Time", "Telecaller", "Customer Name", "Phone", "Result", "Duration (sec)", "Notes"])

    logs = CallLog.objects.select_related("caller", "lead").order_by("-call_time")
    for log in logs:
        writer.writerow([
            log.id,
            log.call_time.strftime("%Y-%m-%d %H:%M:%S"),
            log.caller.get_full_name() or log.caller.username,
            log.lead.name,
            log.lead.phone,
            log.get_call_result_display(),
            log.duration_seconds,
            log.remarks
        ])

    ExportHistory.objects.create(
        user=request.user,
        export_type="Full Call History Log CSV",
        filters_applied={"all_calls": True},
        record_count=logs.count()
    )
    return response


@user_passes_test(is_md, login_url="login")
def export_followup_report_csv(request):
    """Export full follow-up schedule report CSV."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="DreamCadd_Followups_{timezone.now().strftime("%Y%m%d_%H%M")}.csv"'

    writer = csv.writer(response)
    writer.writerow(["Followup ID", "Scheduled Date/Time", "Telecaller", "Customer Name", "Phone", "Status", "Notes", "Created At"])

    followups = Followup.objects.select_related("caller", "lead").order_by("-scheduled_date")
    for f in followups:
        writer.writerow([
            f.id,
            f.scheduled_date.strftime("%Y-%m-%d %H:%M"),
            f.caller.get_full_name() or f.caller.username,
            f.lead.name,
            f.lead.phone,
            f.get_status_display(),
            f.remarks,
            f.created_at.strftime("%Y-%m-%d %H:%M")
        ])

    ExportHistory.objects.create(
        user=request.user,
        export_type="Follow-up Schedule Report CSV",
        filters_applied={"all_followups": True},
        record_count=followups.count()
    )
    return response



@user_passes_test(is_md, login_url="login")
def company_settings_view(request):
    company = CompanySettings.load()
    branches = Branch.objects.all()

    if request.method == "POST":
        if "update_company" in request.POST:
            form = CompanySettingsForm(request.POST, instance=company)
            if form.is_valid():
                form.save()
                messages.success(request, "Company settings updated.")
                return redirect("company_settings")
        elif "add_branch" in request.POST:
            b_form = BranchForm(request.POST)
            if b_form.is_valid():
                b_form.save()
                messages.success(request, "New branch added.")
                return redirect("company_settings")

    c_form = CompanySettingsForm(instance=company)
    b_form = BranchForm()
    return render(request, "leads/company_settings.html", {
        "company": company,
        "branches": branches,
        "c_form": c_form,
        "b_form": b_form,
    })


@user_passes_test(is_md, login_url="login")
def manage_courses(request):
    courses = Course.objects.select_related("category").all()
    if request.method == "POST":
        form = CourseForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, "Course added successfully.")
            return redirect("manage_courses")
    else:
        form = CourseForm()

    return render(request, "leads/manage_courses.html", {"courses": courses, "form": form})


@user_passes_test(is_md, login_url="login")
def whatsapp_templates_view(request):
    templates = WhatsAppTemplate.objects.all().order_by("-created_at")
    if request.method == "POST":
        form = WhatsAppTemplateForm(request.POST)
        if form.is_valid():
            tmpl = form.save(commit=False)
            # Detect variables in body_text
            vars_found = re.findall(r"\{\{([a-zA-Z0-9_]+)\}\}", tmpl.body_text)
            tmpl.variables = list(set(vars_found))
            tmpl.save()
            messages.success(request, f"WhatsApp Template '{tmpl.template_name}' created (Status: {tmpl.status}).")
            return redirect("whatsapp_templates")
    else:
        form = WhatsAppTemplateForm()

    return render(request, "leads/whatsapp_templates.html", {"templates": templates, "form": form})


@user_passes_test(is_md, login_url="login")
def import_history_view(request):
    if request.method == "POST" and "delete_batch" in request.POST:
        batch_id = request.POST.get("batch_id")
        batch = get_object_or_404(ImportBatch, id=batch_id)
        batch_code = batch.batch_code
        batch.delete()
        messages.success(request, f"Import batch '{batch_code}' deleted successfully.")
        return redirect("import_history")

    batches = ImportBatch.objects.select_related("uploaded_by").order_by("-uploaded_at")
    return render(request, "leads/import_history.html", {"batches": batches})


@user_passes_test(is_md, login_url="login")
@require_POST
def unassign_leads(request):
    """
    MD endpoint to unassign leads or an entire allocation batch.
    Returns leads to the UNALLOCATED pool (assigned_to=None), preserves master records, and logs history.
    """
    lead_ids = request.POST.getlist("lead_ids")
    batch_id = request.POST.get("batch_id")

    if batch_id:
        batch = get_object_or_404(AllocationBatch, id=batch_id)
        leads = Lead.objects.filter(allocation_batch=batch)
        batch_name = batch.batch_code
    elif lead_ids:
        leads = Lead.objects.filter(id__in=lead_ids)
        batch_name = f"{leads.count()} selected lead(s)"
    else:
        messages.warning(request, "No leads or batch selected for unassignment.")
        return redirect("bulk_allocate")

    unassigned_count = 0
    for lead in leads:
        old_caller = lead.assigned_to
        lead.assigned_to = None
        lead.allocation_batch = None
        if lead.status == Lead.Status.ASSIGNED:
            lead.status = Lead.Status.NEW
        lead.save(update_fields=["assigned_to", "allocation_batch", "status", "updated_at"])

        if old_caller:
            LeadAssignment.objects.create(lead=lead, caller=old_caller, status=LeadAssignment.Status.CLOSED)
            log_lead_activity(lead, "UNASSIGNED", f"Unassigned from {old_caller.username} by MD", actor=request.user)
        unassigned_count += 1

    messages.success(request, f"Successfully unassigned {unassigned_count} lead(s) ({batch_name}). They are now back in the Unallocated Pool.")
    return redirect("bulk_allocate")


@user_passes_test(is_md, login_url="login")
def export_leads_csv(request):
    """
    Export leads to CSV with optional filters (Caller, Status, Batch, Priority).
    Logs every export event to ExportHistory for audit compliance.
    """
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="dreamcadd_leads_export_{timezone.now().strftime("%Y%m%d_%H%M")}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        "Lead ID", "Customer Name", "Phone", "Email", "Education", "College",
        "Course", "Preferred Branch", "Budget", "Requirement", "Lead Status", "Priority",
        "Assigned Telecaller", "Assigned Date", "Last Call Date", "Last Call Result",
        "Last Call Notes", "Followup Date", "Lead Score", "Created At"
    ])

    leads = Lead.objects.select_related("assigned_to", "interested_course", "preferred_branch").all()

    # Filtering parameters
    caller_id = request.GET.get("caller")
    status_val = request.GET.get("status")
    batch_id = request.GET.get("batch")
    priority_val = request.GET.get("priority")

    if caller_id:
        if caller_id == "unassigned":
            leads = leads.filter(assigned_to__isnull=True)
        else:
            leads = leads.filter(assigned_to_id=caller_id)
    if status_val:
        leads = leads.filter(status=status_val)
    if batch_id:
        leads = leads.filter(allocation_batch_id=batch_id)
    if priority_val:
        leads = leads.filter(priority=priority_val)

    exported_count = 0
    for l in leads:
        last_call = l.call_logs.order_by("-call_time").first()
        last_followup = l.followups.order_by("-scheduled_date").first()

        writer.writerow([
            l.id,
            l.name,
            l.phone,
            l.email or "",
            l.education if l.education and l.education.lower() not in ["nan", "none", "unspecified"] else "",
            l.college if l.college and l.college.lower() not in ["nan", "none", "unspecified"] else "",
            l.interested_course.course_name if l.interested_course else "",
            l.preferred_branch.branch_name if l.preferred_branch else "",
            l.budget or "",
            l.requirement_note or "",
            l.get_status_display(),
            l.priority,
            l.assignee_display,
            l.assigned_at.strftime("%Y-%m-%d %H:%M") if l.assigned_at else "",
            last_call.call_time.strftime("%Y-%m-%d %H:%M") if last_call else "",
            last_call.get_call_result_display() if last_call else "",
            last_call.remarks if last_call else "",
            last_followup.scheduled_date.strftime("%Y-%m-%d %H:%M") if last_followup else "",
            l.lead_score,
            l.created_at.strftime("%Y-%m-%d %H:%M")
        ])
        exported_count += 1

    # Log export history for audit compliance
    ExportHistory.objects.create(
        user=request.user,
        export_type="Filtered Lead CSV",
        filters_applied={
            "caller": caller_id or "All",
            "status": status_val or "All",
            "batch": batch_id or "All",
            "priority": priority_val or "All"
        },
        record_count=exported_count
    )

    return response


@user_passes_test(is_md, login_url="login")
def export_history_view(request):
    exports = ExportHistory.objects.select_related("user").order_by("-exported_at")
    return render(request, "leads/export_history.html", {"exports": exports})



# ---------------------------------------------------------------------------
# Telecaller & Detail Views
# ---------------------------------------------------------------------------

@user_passes_test(is_telecaller, login_url="login")
def telecaller_dashboard(request):
    leads = Lead.objects.filter(assigned_to=request.user).select_related("interested_course").order_by("-updated_at")
    pending_followups = Followup.objects.filter(caller=request.user, status=Followup.Status.PENDING).select_related("lead").order_by("scheduled_date")
    notifications = Notification.objects.filter(user=request.user, is_read=False)[:10]

    today = timezone.now().date()
    assigned_count = leads.count()
    unique_contacted_count = leads.filter(contacted=True).count()
    total_call_attempts = CallLog.objects.filter(caller=request.user).count()
    today_call_attempts = CallLog.objects.filter(caller=request.user, created_at__date=today).count()

    interested_count = leads.filter(status=Lead.Status.INTERESTED).count()
    converted_count = leads.filter(status=Lead.Status.CONVERTED).count()
    not_answered_count = leads.filter(status__in=[Lead.Status.NOT_ANSWERED, Lead.Status.NOT_CONNECTED]).count()
    call_back_count = leads.filter(status=Lead.Status.CALL_BACK).count()

    return render(request, "leads/telecaller_dashboard.html", {
        "leads": leads,
        "pending_followups": pending_followups,
        "notifications": notifications,
        "assigned_count": assigned_count,
        "unique_contacted_count": unique_contacted_count,
        "total_call_attempts": total_call_attempts,
        "today_call_attempts": today_call_attempts,
        "today_calls": today_call_attempts,
        "total_calls": total_call_attempts,
        "connected_count": unique_contacted_count,
        "converted_count": converted_count,
        "interested_count": interested_count,
        "not_answered_count": not_answered_count,
        "call_back_count": call_back_count,
    })



@login_required
def lead_detail(request, lead_id):
    lead = get_object_or_404(Lead, id=lead_id)

    if request.user.is_telecaller and lead.assigned_to_id != request.user.id:
        return HttpResponseForbidden("Access Denied: This lead is not assigned to you.")

    templates = WhatsAppTemplate.objects.filter(status=WhatsAppTemplate.Status.APPROVED)
    activities = lead.activities.select_related("actor").all()[:20]
    call_logs = lead.call_logs.select_related("caller").all()

    if request.method == "POST":
        if "log_followup" in request.POST:
            f_form = FollowupForm(request.POST)
            if f_form.is_valid():
                followup = f_form.save(commit=False)
                followup.lead = lead
                followup.caller = request.user
                followup.save()

                log_lead_activity(lead, "FOLLOWUP_SCHEDULED", f"Follow-up scheduled for {followup.scheduled_date.strftime('%d-%b-%Y %H:%M')}", actor=request.user)
                messages.success(request, "Follow-up scheduled successfully.")

        elif "log_call" in request.POST:
            c_form = CallLogForm(request.POST)
            if c_form.is_valid():
                call_log = c_form.save(commit=False)
                call_log.lead = lead
                call_log.caller = request.user
                call_log.save()

                # Automatic status mapping based on Call Result
                res_map = {
                    CallLog.Result.INTERESTED: Lead.Status.INTERESTED,
                    CallLog.Result.CONVERTED: Lead.Status.CONVERTED,
                    CallLog.Result.NOT_INTERESTED: Lead.Status.NOT_INTERESTED,
                    CallLog.Result.CALLBACK_REQUESTED: Lead.Status.CALL_BACK,
                    CallLog.Result.BUSY: Lead.Status.BUSY,
                    CallLog.Result.NOT_CONNECTED: Lead.Status.NOT_CONNECTED,
                    CallLog.Result.SWITCHED_OFF: Lead.Status.SWITCHED_OFF,
                    CallLog.Result.WRONG_NUMBER: Lead.Status.WRONG_NUMBER,
                    CallLog.Result.CONNECTED: Lead.Status.CONTACTED,
                }
                if call_log.call_result in res_map:
                    lead.status = res_map[call_log.call_result]
                    lead.save(update_fields=["status", "updated_at"])

                # Optional follow-up creation from call modal
                followup_date_str = request.POST.get("next_followup_date")
                if followup_date_str:
                    try:
                        f_date = timezone.datetime.fromisoformat(followup_date_str)
                        Followup.objects.create(
                            lead=lead,
                            caller=request.user,
                            scheduled_date=f_date,
                            remarks=f"Scheduled during call log ({call_log.call_result})"
                        )
                    except Exception:
                        pass

                log_lead_activity(lead, "CALLED", f"Call logged ({call_log.get_call_result_display()}, {call_log.duration_seconds}s)", actor=request.user)
                messages.success(request, f"Call log saved ({call_log.get_call_result_display()}). Lead status updated to {lead.get_status_display()}.")

        elif "send_template_wa" in request.POST:
            tmpl_id = request.POST.get("template_id")
            tmpl = WhatsAppTemplate.objects.filter(id=tmpl_id).first()
            if tmpl:
                msg_text = render_whatsapp_template(tmpl, lead)
                send_whatsapp_text(lead, msg_text)
                messages.success(request, "Template message sent via WhatsApp.")

        elif "send_custom_wa" in request.POST:
            text = request.POST.get("message_text", "").strip()
            if text:
                send_whatsapp_text(lead, text)
                messages.success(request, "Custom WhatsApp message sent.")

        elif "update_lead_info" in request.POST:
            l_form = LeadForm(request.POST, instance=lead)
            if l_form.is_valid():
                l_form.save()
                log_lead_activity(lead, "STATUS_CHANGE", f"Lead updated. Status: {lead.status}, Priority: {lead.priority}", actor=request.user)
                messages.success(request, "Lead information updated.")

        return redirect("lead_detail", lead_id=lead.id)

    last_call = lead.call_logs.order_by("-call_time").first()
    next_followup = lead.followups.order_by("-scheduled_date").first()

    return render(request, "leads/lead_detail.html", {
        "lead": lead,
        "calls_count": call_logs.count(),
        "last_call": last_call,
        "next_followup": next_followup,
        "followups": lead.followups.select_related("caller").order_by("-scheduled_date"),
        "call_logs": call_logs.order_by("-call_time")[:20],
        "wa_messages": lead.whatsapp_messages.order_by("timestamp"),
        "activities": activities,
        "templates": templates,
        "courses": Course.objects.all(),
        "branches": Branch.objects.all(),
        "followup_form": FollowupForm(),
        "lead_form": LeadForm(instance=lead),
    })


# ---------------------------------------------------------------------------
# Clear / Reset Database (MD Only)
# ---------------------------------------------------------------------------

@user_passes_test(is_md, login_url="login")
@require_POST
def clear_database(request):
    """Permanently delete ALL lead-related data from the database.
    Requires the MD to type 'DELETE' in the confirmation field.
    """
    confirmation = request.POST.get("confirm_text", "").strip()
    if confirmation != "DELETE":
        messages.error(request, "Incorrect confirmation. Type DELETE exactly to proceed.")
        return redirect("md_dashboard")

    # Delete in dependency order to avoid FK constraint issues
    LeadActivity.objects.all().delete()
    Followup.objects.all().delete()
    CallLog.objects.all().delete()
    WhatsAppMessage.objects.all().delete()
    LeadAssignment.objects.all().delete()
    Lead.objects.all().delete()
    ImportBatch.objects.all().delete()

    messages.success(request, "✅ All lead data has been permanently cleared from the database.")
    return redirect("md_dashboard")


# ---------------------------------------------------------------------------
# Direct Meta WhatsApp Webhook
# ---------------------------------------------------------------------------

@csrf_exempt
def whatsapp_webhook(request):
    if request.method == "GET":
        verify_token = getattr(settings, "WHATSAPP_VERIFY_TOKEN", "")
        if (request.GET.get("hub.mode") == "subscribe"
                and request.GET.get("hub.verify_token") == verify_token):
            return HttpResponse(request.GET.get("hub.challenge", ""))
        return HttpResponseForbidden("Verification failed")

    if request.method == "POST":
        if not verify_meta_webhook_signature(request):
            return HttpResponseForbidden("Invalid HMAC signature")

        try:
            payload = json.loads(request.body.decode("utf-8"))
            entry = payload.get("entry", [{}])[0]
            change = entry.get("changes", [{}])[0]
            value = change.get("value", {})

            for msg in value.get("messages", []):
                phone = msg.get("from")
                text = msg.get("text", {}).get("body", "")
                if phone and text:
                    process_incoming_whatsapp(phone, text)

        except Exception:
            pass
        return JsonResponse({"status": "ok"})

    return HttpResponse(status=405)


@login_required
@require_POST
def api_quick_call_log(request, lead_id):
    """
    Mobile-first quick response endpoint.
    One-tap response submission from bottom sheet.
    Creates CallLog, updates Lead status, logs activity, and sets up optional Follow-Up.
    """
    lead = get_object_or_404(Lead, id=lead_id)

    # Telecaller data isolation check
    if request.user.is_telecaller and lead.assigned_to_id != request.user.id:
        return JsonResponse({"status": "error", "message": "Access Denied: Lead is not assigned to you."}, status=403)

    if request.content_type == "application/json":
        try:
            data = json.loads(request.body.decode("utf-8"))
        except Exception:
            data = request.POST
    else:
        data = request.POST

    outcome = data.get("outcome", "").upper()
    notes = data.get("notes", "").strip()
    call_duration = data.get("call_duration", 0)
    followup_date = data.get("followup_date", "").strip()
    followup_time = data.get("followup_time", "").strip()
    followup_note = data.get("followup_note", "").strip()
    call_session_id = data.get("call_session_id", "").strip()

    # Strict Idempotency Enforcement: Prevent duplicate call logs/counts for same calling session
    if call_session_id:
        existing_log = CallLog.objects.filter(call_session_id=call_session_id).first()
        if existing_log:
            next_fu = lead.followups.order_by("-scheduled_date").first()
            fu_str = next_fu.scheduled_date.strftime("%d %b %Y, %H:%M") if next_fu else ""
            return JsonResponse({
                "status": "success",
                "message": f"Recorded '{existing_log.get_call_result_display()}' for {lead.name}.",
                "lead_id": lead.id,
                "new_status": lead.status,
                "new_status_display": lead.get_status_display(),
                "call_log_id": existing_log.id,
                "followup_date": fu_str,
                "updated_at": lead.updated_at.strftime("%d %b %Y, %H:%M"),
                "idempotent": True
            })

    outcomes_map = {
        "NOT_ANSWERED": (CallLog.Result.NOT_ANSWERED, Lead.Status.NOT_CONNECTED),
        "CONNECTED": (CallLog.Result.CONNECTED, Lead.Status.CONTACTED),
        "NOT_CONNECTED": (CallLog.Result.NOT_CONNECTED, Lead.Status.NOT_CONNECTED),
        "NOT_ATTENDED": (CallLog.Result.NOT_CONNECTED, Lead.Status.NOT_CONNECTED),
        "BUSY": (CallLog.Result.BUSY, Lead.Status.BUSY),
        "SWITCHED_OFF": (CallLog.Result.SWITCHED_OFF, Lead.Status.SWITCHED_OFF),
        "WRONG_NUMBER": (CallLog.Result.WRONG_NUMBER, Lead.Status.WRONG_NUMBER),
        "INVALID_NUMBER": (CallLog.Result.INVALID_NUMBER, Lead.Status.DROPPED),
        "NO_RESPONSE": (CallLog.Result.NO_RESPONSE, Lead.Status.NOT_CONNECTED),
        "INTERESTED": (CallLog.Result.INTERESTED, Lead.Status.INTERESTED),
        "NOT_INTERESTED": (CallLog.Result.NOT_INTERESTED, Lead.Status.NOT_INTERESTED),
        "CALL_BACK": (CallLog.Result.CALLBACK_REQUESTED, Lead.Status.CALL_BACK),
        "CONVERTED": (CallLog.Result.CONVERTED, Lead.Status.CONVERTED),
    }

    if outcome not in outcomes_map:
        return JsonResponse({"status": "error", "message": f"Invalid call outcome '{outcome}'."}, status=400)

    call_result, new_lead_status = outcomes_map[outcome]

    # Create permanent CallLog record
    call_log = CallLog.objects.create(
        lead=lead,
        caller=request.user,
        call_result=call_result,
        duration_seconds=int(call_duration or 0),
        remarks=notes or f"Quick response logged: {call_result}",
        call_session_id=call_session_id if call_session_id else None
    )

    # Update Lead status, contact tracking & optional Interested fields
    course_id = data.get("course_id")
    preferred_branch_id = data.get("preferred_branch_id")
    budget = data.get("budget", "").strip()
    requirement_note = data.get("requirement_note", "").strip()

    now = timezone.now()
    update_fields = ["status", "last_call_at", "current_response", "updated_at"]
    lead.status = new_lead_status
    lead.last_call_at = now
    lead.current_response = call_log.get_call_result_display()

    if not lead.contacted:
        lead.contacted = True
        lead.first_contacted_at = now
        update_fields.extend(["contacted", "first_contacted_at"])

    if course_id:
        lead.interested_course_id = course_id
        update_fields.append("interested_course")
    if preferred_branch_id:
        lead.preferred_branch_id = preferred_branch_id
        update_fields.append("preferred_branch")
    if budget:
        lead.budget = budget
        update_fields.append("budget")
    if requirement_note:
        lead.requirement_note = requirement_note
        update_fields.append("requirement_note")

    lead.save(update_fields=update_fields)

    # Follow-Up creation if date supplied or outcome is INTERESTED/CALL_BACK
    followup_scheduled_str = ""
    if followup_date:
        try:
            dt_str = f"{followup_date} {followup_time or '10:00'}"
            scheduled_dt = timezone.datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            if timezone.is_naive(scheduled_dt):
                scheduled_dt = timezone.make_aware(scheduled_dt, timezone.get_current_timezone())

            Followup.objects.create(
                lead=lead,
                caller=request.user,
                scheduled_date=scheduled_dt,
                remarks=followup_note or f"Scheduled via mobile quick-response ({call_log.get_call_result_display()})"
            )
            followup_scheduled_str = scheduled_dt.strftime("%d %b %Y, %H:%M")
        except Exception:
            pass

    # Log activity
    act_desc = f"Call logged: {call_log.get_call_result_display()}"
    if followup_scheduled_str:
        act_desc += f" | Follow-up set for {followup_scheduled_str}"
    log_lead_activity(lead, "QUICK_CALL_LOG", act_desc, actor=request.user)

    # Update session log last activity
    try:
        user_sess = UserSessionLog.objects.filter(user=request.user, logout_time__isnull=True).first()
        if user_sess:
            user_sess.last_activity = timezone.now()
            user_sess.save(update_fields=["last_activity"])
    except Exception:
        pass

    return JsonResponse({
        "status": "success",
        "message": f"Recorded '{call_log.get_call_result_display()}' for {lead.name}.",
        "lead_id": lead.id,
        "new_status": lead.status,
        "new_status_display": lead.get_status_display(),
        "call_log_id": call_log.id,
        "followup_date": followup_scheduled_str,
        "updated_at": lead.updated_at.strftime("%d %b %Y, %H:%M")
    })


@login_required
def api_session_check(request):
    """
    Realtime session health & system change tracker endpoint.
    Verifies active session & authentication status across all pages.
    Returns 401 Unauthorized if user is inactive, disabled, or unauthenticated.
    Also returns system modification timestamp to trigger ASAP frontend updates.
    """
    if not request.user.is_authenticated or not request.user.is_active or getattr(request.user, "status", "ACTIVE") == "INACTIVE":
        return JsonResponse({"status": "inactive", "authenticated": False}, status=401)

    return JsonResponse({
        "status": "active",
        "authenticated": True,
        "username": request.user.username,
        "role": request.user.role,
        "last_changed": get_system_last_changed(),
        "timestamp": timezone.now().strftime("%Y-%m-%d %H:%M:%S")
    })


@csrf_exempt
@login_required
@require_POST
def api_edit_response(request, lead_id):
    """
    Allows a Telecaller or MD to update a customer's response status.
    Does NOT increment unique customer count or create duplicate leads.
    """
    lead = get_object_or_404(Lead, id=lead_id)

    # Permission check: assigned telecaller, MD, or admin
    if not (request.user.is_md or lead.assigned_to == request.user or request.user.is_superuser):
        return JsonResponse({"status": "error", "message": "Permission denied."}, status=403)

    if request.content_type == "application/json":
        data = json.loads(request.body.decode("utf-8"))
    else:
        data = request.POST

    new_response = data.get("new_response", "").upper().strip()
    notes = data.get("notes", "").strip()

    status_map = {
        "INTERESTED": (Lead.Status.INTERESTED, "Interested 🟢"),
        "NOT_INTERESTED": (Lead.Status.NOT_INTERESTED, "Not Interested 🔴"),
        "CALL_BACK": (Lead.Status.CALL_BACK, "Call Back 📞"),
        "CONVERTED": (Lead.Status.CONVERTED, "Converted 🎉"),
        "NOT_ANSWERED": (Lead.Status.NOT_CONNECTED, "Not Answered 📵"),
        "BUSY": (Lead.Status.BUSY, "Busy ⌛"),
        "SWITCHED_OFF": (Lead.Status.SWITCHED_OFF, "Switched Off 📱"),
        "WRONG_NUMBER": (Lead.Status.WRONG_NUMBER, "Wrong Number ❌"),
        "DROPPED": (Lead.Status.DROPPED, "Dropped ❌"),
    }

    if new_response not in status_map:
        return JsonResponse({"status": "error", "message": f"Invalid response option '{new_response}'."}, status=400)

    old_response = lead.current_response or lead.get_status_display()
    new_status, display_name = status_map[new_response]

    lead.status = new_status
    lead.current_response = display_name
    if not lead.contacted:
        lead.contacted = True
        lead.first_contacted_at = timezone.now()
    lead.save(update_fields=["status", "current_response", "contacted", "first_contacted_at", "updated_at"])

    # Log Activity
    log_lead_activity(
        lead=lead,
        actor=request.user,
        activity_type="RESPONSE_UPDATED",
        description=f"Response updated from '{old_response}' to '{display_name}'. {notes}".strip()
    )

    return JsonResponse({
        "status": "success",
        "message": f"Updated response for {lead.name} to {display_name}.",
        "lead_id": lead.id,
        "new_status": lead.status,
        "new_status_display": lead.get_status_display(),
        "new_response": display_name,
        "updated_at": lead.updated_at.strftime("%d %b %Y, %H:%M")
    })

