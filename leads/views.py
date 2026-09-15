import csv
import re
import json
from datetime import datetime, timedelta
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.views import LoginView
from django.contrib.sessions.models import Session
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .api_security import verify_meta_webhook_signature
from .forms import (
    BranchForm, CallLogForm, ChangePasswordForm, ChangeUsernameForm,
    CompanySettingsForm, CourseForm, ExcelUploadForm, FollowupForm, LeadForm,
    StyledAuthenticationForm, TelecallerCreateForm, WhatsAppTemplateForm
)
from .models import (
    AllocationBatch, Branch, CallLog, CompanySettings, Course, DailySchedule, ExportHistory, Followup, ImportBatch, Lead,
    LeadActivity, LeadAssignment, LeadSource, Notification, SecurityAuditLog,
    TelecallerProfile, User, UserSessionLog, WhatsAppMessage, WhatsAppTemplate, log_security_event
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
    return user.is_authenticated and user.role == User.Role.TELECALLER


class RoleAwareLoginView(LoginView):
    template_name = "leads/login.html"
    authentication_form = StyledAuthenticationForm

    def form_valid(self, form):
        response = super().form_valid(form)
        log_security_event(self.request.user, SecurityAuditLog.Action.LOGIN, self.request, details="User logged in successfully")
        return response

    def form_invalid(self, form):
        import logging
        logger = logging.getLogger(__name__)
        username = form.cleaned_data.get('username', 'unknown') if hasattr(form, 'cleaned_data') and form.cleaned_data else 'unknown'
        logger.warning(f"Login failed for user '{username}': {form.errors.as_text()}")
        messages.error(self.request, "Invalid username or password. Please try again.")
        return super().form_invalid(form)

    def get_success_url(self):
        redirect_to = self.get_redirect_url()
        user = self.request.user

        if redirect_to and not redirect_to.startswith('/login'):
            if user.is_md and '/telecaller' in redirect_to:
                return reverse_lazy("md_dashboard")
            if not user.is_md and '/md' in redirect_to:
                return reverse_lazy("telecaller_dashboard")
            return redirect_to

        if user.is_md:
            return reverse_lazy("md_dashboard")
        return reverse_lazy("telecaller_dashboard")



@login_required
def home_redirect(request):
    if request.user.is_md:
        return redirect("md_dashboard")
    return redirect("telecaller_dashboard")


def custom_csrf_failure_view(request, reason=""):
    """Handle CSRF failure gracefully by redirecting back to login with a friendly message."""
    messages.warning(request, "Your security token or session expired. Please sign in again.")
    return redirect("login")


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
        unique_contacted_count=Count("assigned_leads", filter=Q(assigned_leads__contacted=True), distinct=True),
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
        elif "update_caller_credentials" in request.POST:
            caller_id = request.POST.get("caller_id")
            caller = get_object_or_404(User, id=caller_id, role=User.Role.TELECALLER)
            new_username = request.POST.get("new_username", "").strip()
            new_password = request.POST.get("new_password", "").strip()

            updated_fields = []
            old_username = caller.username

            if new_username and new_username.lower() != old_username.lower():
                if User.objects.filter(username__iexact=new_username).exclude(pk=caller.pk).exists():
                    messages.error(request, f"Username '{new_username}' is already taken by another account.")
                    return redirect("manage_callers")
                caller.username = new_username
                updated_fields.append(f"username to '{new_username}'")

            if new_password:
                if len(new_password) < 6:
                    messages.error(request, "Password must be at least 6 characters long.")
                    return redirect("manage_callers")
                caller.set_password(new_password)
                updated_fields.append("password")

            if updated_fields:
                caller.save()
                log_security_event(
                    user=request.user,
                    action=SecurityAuditLog.Action.USERNAME_CHANGED if "username" in updated_fields[0] else SecurityAuditLog.Action.PASSWORD_CHANGED,
                    request=request,
                    details=f"Admin updated credentials for telecaller '{old_username}': {', '.join(updated_fields)}"
                )
                messages.success(request, f"Credentials updated for telecaller '{old_username}': {', '.join(updated_fields)}.")
            else:
                messages.info(request, f"No credentials changes were submitted for telecaller '{caller.username}'.")
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
def schedule_leads_view(request):
    """Admin / MD Workbench to schedule lead lists in advance for each telecaller per day & define daily call targets."""
    telecallers = User.objects.filter(role=User.Role.TELECALLER, status="ACTIVE").select_related("profile")
    courses = Course.objects.all()

    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "create_schedule":
            caller_id = request.POST.get("caller_id")
            target_date_str = request.POST.get("target_date")
            call_target_str = request.POST.get("daily_call_target", "30")
            conversion_target_str = request.POST.get("daily_conversion_target", "5")
            lead_count_str = request.POST.get("lead_count", "30")
            allocation_mode = request.POST.get("allocation_mode", "UNASSIGNED")
            course_id = request.POST.get("course_id", "")
            notes = request.POST.get("notes", "").strip()

            if not caller_id or not target_date_str:
                messages.error(request, "Please select both a Telecaller and a Target Date.")
                return redirect("schedule_leads")

            try:
                target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
                call_target = max(1, int(call_target_str))
                conversion_target = max(0, int(conversion_target_str))
                lead_count = max(0, int(lead_count_str))
            except ValueError:
                messages.error(request, "Invalid input numbers or date format.")
                return redirect("schedule_leads")

            caller = get_object_or_404(User, id=caller_id, role=User.Role.TELECALLER)

            # Create or update DailySchedule
            schedule, created = DailySchedule.objects.update_or_create(
                caller=caller,
                target_date=target_date,
                defaults={
                    "daily_call_target": call_target,
                    "daily_conversion_target": conversion_target,
                    "notes": notes,
                    "created_by": request.user,
                }
            )

            # Update TelecallerProfile default target
            profile = caller.telecaller_profile
            profile.daily_call_target = call_target
            profile.save(update_fields=["daily_call_target"])

            scheduled_leads_count = 0
            if lead_count > 0:
                if allocation_mode == "UNASSIGNED":
                    qs = Lead.objects.filter(assigned_to__isnull=True)
                    if course_id:
                        qs = qs.filter(interested_course_id=course_id)
                    leads_to_assign = list(qs[:lead_count])
                    if leads_to_assign:
                        batch_code = f"SCHED-{target_date.strftime('%Y%m%d')}-{caller.username[:10].upper()}"
                        alloc_batch = AllocationBatch.objects.create(
                            batch_code=batch_code,
                            caller=caller,
                            allocated_by=request.user,
                            lead_count=len(leads_to_assign),
                            notes=f"Scheduled allocation for {target_date.strftime('%d-%b-%Y')}"
                        )
                        for l in leads_to_assign:
                            l.assigned_to = caller
                            l.scheduled_date = target_date
                            l.allocation_batch = alloc_batch
                            l.assigned_at = timezone.now()
                            if l.status == Lead.Status.NEW:
                                l.status = Lead.Status.ASSIGNED
                            l.save(update_fields=["assigned_to", "scheduled_date", "allocation_batch", "assigned_at", "status", "updated_at"])
                            LeadAssignment.objects.create(lead=l, caller=caller)
                            log_lead_activity(l, "SCHEDULED", f"Scheduled for {caller.username} on {target_date.strftime('%d-%b-%Y')}", actor=request.user)
                            scheduled_leads_count += 1
                else:  # EXISTING assigned leads of caller
                    qs = Lead.objects.filter(assigned_to=caller, contacted=False)
                    if course_id:
                        qs = qs.filter(interested_course_id=course_id)
                    leads_to_assign = list(qs[:lead_count])
                    for l in leads_to_assign:
                        l.scheduled_date = target_date
                        l.save(update_fields=["scheduled_date", "updated_at"])
                        log_lead_activity(l, "RESCHEDULED", f"Rescheduled call date to {target_date.strftime('%d-%b-%Y')}", actor=request.user)
                        scheduled_leads_count += 1

            messages.success(
                request,
                f"Schedule saved for '{caller.get_full_name() or caller.username}' on {target_date.strftime('%d-%b-%Y')}. "
                f"Daily Target set to {call_target} calls. {scheduled_leads_count} lead(s) scheduled for this date."
            )
            return redirect("schedule_leads")

        elif action == "delete_schedule":
            sched_id = request.POST.get("schedule_id")
            if sched_id:
                sched = DailySchedule.objects.filter(id=sched_id).first()
                if sched:
                    sched_date = sched.target_date
                    caller_name = sched.caller.username
                    sched.delete()
                    messages.success(request, f"Daily schedule for {caller_name} on {sched_date.strftime('%d-%b-%Y')} deleted.")
            return redirect("schedule_leads")

    # GET: Load master schedule list with call progress statistics
    schedules_qs = DailySchedule.objects.select_related("caller", "created_by").order_by("-target_date", "caller")

    # Filter parameters
    selected_date_str = request.GET.get("date", "")
    selected_caller_id = request.GET.get("caller", "")

    if selected_date_str:
        try:
            sel_d = datetime.strptime(selected_date_str, "%Y-%m-%d").date()
            schedules_qs = schedules_qs.filter(target_date=sel_d)
        except ValueError:
            pass

    if selected_caller_id:
        schedules_qs = schedules_qs.filter(caller_id=selected_caller_id)

    schedules_data = []
    for sched in schedules_qs[:100]:
        scheduled_leads_count = Lead.objects.filter(assigned_to=sched.caller, scheduled_date=sched.target_date).count()
        calls_completed = CallLog.objects.filter(caller=sched.caller, created_at__date=sched.target_date).count()
        conversions = Lead.objects.filter(assigned_to=sched.caller, scheduled_date=sched.target_date, status=Lead.Status.CONVERTED).count()

        progress_pct = min(100, int((calls_completed / sched.daily_call_target) * 100)) if sched.daily_call_target > 0 else 0

        schedules_data.append({
            "schedule": sched,
            "scheduled_leads_count": scheduled_leads_count,
            "calls_completed": calls_completed,
            "conversions": conversions,
            "progress_pct": progress_pct,
            "is_target_met": calls_completed >= sched.daily_call_target,
        })

    unassigned_leads_count = Lead.objects.filter(assigned_to__isnull=True).count()
    today_str = timezone.now().date().strftime("%Y-%m-%d")
    tomorrow_str = (timezone.now().date() + timedelta(days=1)).strftime("%Y-%m-%d")

    return render(request, "leads/schedule_leads.html", {
        "telecallers": telecallers,
        "courses": courses,
        "schedules_data": schedules_data,
        "unassigned_leads_count": unassigned_leads_count,
        "today_str": today_str,
        "tomorrow_str": tomorrow_str,
        "selected_date": selected_date_str,
        "selected_caller": selected_caller_id,
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
    unique_contacted_count = leads.filter(contacted=True).count()
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
        "unique_contacted_count": unique_contacted_count,
        "completed_count": completed_count,
        "pending_count": pending_count,
        "calls_made": calls_made,
        "total_call_attempts": calls_made,
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
@login_required
def telecaller_dashboard(request):
    leads_qs = Lead.objects.filter(assigned_to=request.user).select_related("interested_course").order_by("-updated_at")
    pending_followups = Followup.objects.filter(caller=request.user, status=Followup.Status.PENDING).select_related("lead").order_by("scheduled_date")
    notifications = Notification.objects.filter(user=request.user, is_read=False)[:10]

    followup_map = {}
    for f in pending_followups:
        if f.lead_id not in followup_map:
            followup_map[f.lead_id] = f

    active_followup_lead_ids = set(followup_map.keys())

    leads_list = list(leads_qs)
    for lead in leads_list:
        lead.active_followup = followup_map.get(lead.id)

    today = timezone.now().date()
    assigned_count = len(leads_list)
    unique_contacted_count = sum(1 for l in leads_list if l.contacted)
    total_call_attempts = CallLog.objects.filter(caller=request.user).count()
    today_call_attempts = CallLog.objects.filter(caller=request.user, created_at__date=today).count()

    # Daily Schedule & Daily Target Calculations
    today_schedule = DailySchedule.objects.filter(caller=request.user, target_date=today).first()
    if today_schedule:
        daily_call_target = today_schedule.daily_call_target
        daily_conversion_target = today_schedule.daily_conversion_target
    else:
        daily_call_target = request.user.telecaller_profile.daily_call_target
        daily_conversion_target = 5

    remaining_call_target = max(0, daily_call_target - today_call_attempts)
    target_progress_pct = min(100, int((today_call_attempts / daily_call_target) * 100)) if daily_call_target > 0 else 0

    # Today's Scheduled Leads Queue & Upcoming Schedules
    today_scheduled_leads = [l for l in leads_list if l.scheduled_date == today]
    upcoming_schedules = DailySchedule.objects.filter(caller=request.user, target_date__gt=today).order_by("target_date")[:5]

    hot_count = sum(1 for l in leads_list if l.priority == Lead.Priority.HOT)
    pending_leads_count = sum(1 for l in leads_list if l.status in [Lead.Status.NEW, Lead.Status.ASSIGNED])
    interested_count = sum(1 for l in leads_list if l.status == Lead.Status.INTERESTED)
    converted_count = sum(1 for l in leads_list if l.status == Lead.Status.CONVERTED)
    followup_count = len(active_followup_lead_ids)
    not_answered_count = sum(1 for l in leads_list if l.status in [Lead.Status.NOT_CONNECTED, Lead.Status.NOT_ANSWERED, Lead.Status.SWITCHED_OFF, Lead.Status.BUSY])
    call_back_count = sum(1 for l in leads_list if l.status == Lead.Status.CALL_BACK)

    return render(request, "leads/telecaller_dashboard.html", {
        "leads": leads_list,
        "pending_followups": pending_followups,
        "notifications": notifications,
        "assigned_count": assigned_count,
        "unique_contacted_count": unique_contacted_count,
        "total_call_attempts": total_call_attempts,
        "today_call_attempts": today_call_attempts,
        "today_calls": today_call_attempts,
        "total_calls": total_call_attempts,
        "connected_count": unique_contacted_count,
        "hot_count": hot_count,
        "pending_leads_count": pending_leads_count,
        "interested_count": interested_count,
        "converted_count": converted_count,
        "followup_count": followup_count,
        "not_answered_count": not_answered_count,
        "call_back_count": call_back_count,
        "today_schedule": today_schedule,
        "daily_call_target": daily_call_target,
        "daily_conversion_target": daily_conversion_target,
        "remaining_call_target": remaining_call_target,
        "target_progress_pct": target_progress_pct,
        "today_scheduled_leads": today_scheduled_leads,
        "upcoming_schedules": upcoming_schedules,
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


@login_required
def lead_whatsapp(request, lead_id):
    """Dedicated WhatsApp Hub page for a lead with high-converting prepared messages & live chat history."""
    lead = get_object_or_404(Lead, id=lead_id)

    if request.user.is_telecaller and lead.assigned_to_id != request.user.id:
        return HttpResponseForbidden("Access Denied: This lead is not assigned to you.")

    templates = WhatsAppTemplate.objects.filter(status=WhatsAppTemplate.Status.APPROVED)
    wa_messages = lead.whatsapp_messages.order_by("timestamp")

    return render(request, "leads/lead_whatsapp.html", {
        "lead": lead,
        "templates": templates,
        "wa_messages": wa_messages,
        "courses": Course.objects.all(),
        "branches": Branch.objects.all(),
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


@login_required
@require_POST
def api_send_whatsapp(request, lead_id):
    """
    Sends outbound WhatsApp message (template or custom text) for a lead.
    Strict Permission Enforcement: Telecallers can ONLY send WhatsApp to leads assigned to them.
    """
    lead = get_object_or_404(Lead, id=lead_id)

    # Permission check: Telecaller can ONLY access & send WhatsApp for assigned leads
    if request.user.is_telecaller and lead.assigned_to_id != request.user.id:
        return JsonResponse({
            "status": "error",
            "message": "Access Denied: You can only send WhatsApp messages to your assigned customers."
        }, status=403)

    try:
        data = json.loads(request.body.decode("utf-8")) if request.content_type == "application/json" else request.POST
        template_id = data.get("template_id")
        custom_message = data.get("message", "").strip()

        if template_id:
            tmpl = get_object_or_404(WhatsAppTemplate, id=template_id, status=WhatsAppTemplate.Status.APPROVED)
            message_text = render_whatsapp_template(tmpl, lead)
        elif custom_message:
            message_text = custom_message
        else:
            return JsonResponse({"status": "error", "message": "Message text or approved template ID required."}, status=400)

        record = send_whatsapp_text(lead, message_text)

        return JsonResponse({
            "status": "success",
            "message": "WhatsApp message dispatched successfully.",
            "wa_message": {
                "id": record.id,
                "message": record.message,
                "direction": record.direction,
                "status": record.status,
                "timestamp": record.timestamp.strftime("%d %b, %H:%M"),
            }
        })
    except Exception as exc:
        return JsonResponse({"status": "error", "message": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# Secure Admin/MD Account Management Views
# ---------------------------------------------------------------------------

def _get_active_user_sessions_count(user):
    now = timezone.now()
    count = 0
    for session in Session.objects.filter(expire_date__gte=now):
        try:
            data = session.get_decoded()
            if str(data.get("_auth_user_id")) == str(user.pk):
                count += 1
        except Exception:
            pass
    return max(count, 1)


def _logout_other_user_sessions(user, current_session_key):
    now = timezone.now()
    count = 0
    for session in Session.objects.filter(expire_date__gte=now):
        if session.session_key != current_session_key:
            try:
                data = session.get_decoded()
                if str(data.get("_auth_user_id")) == str(user.pk):
                    session.delete()
                    count += 1
            except Exception:
                pass
    return count


@login_required
def account_settings(request):
    """
    Renders the MD/Admin My Account page.
    Telecallers are blocked with 403 Forbidden.
    """
    if not request.user.is_md:
        return HttpResponseForbidden("Access denied. Only MD/Admin can access account settings.")

    username_form = ChangeUsernameForm(user=request.user)
    password_form = ChangePasswordForm(user=request.user)
    active_sessions_count = _get_active_user_sessions_count(request.user)

    context = {
        "username_form": username_form,
        "password_form": password_form,
        "active_sessions_count": active_sessions_count,
    }
    return render(request, "leads/my_account.html", context)


@login_required
@require_POST
def change_username_view(request):
    """
    Updates the authenticated MD/Admin username securely.
    """
    if not request.user.is_md:
        return HttpResponseForbidden("Access denied. Only MD/Admin can change username.")

    username_form = ChangeUsernameForm(user=request.user, data=request.POST)
    password_form = ChangePasswordForm(user=request.user)
    active_sessions_count = _get_active_user_sessions_count(request.user)

    if username_form.is_valid():
        old_username = request.user.username
        new_username = username_form.cleaned_data["new_username"]
        request.user.username = new_username
        request.user.save(update_fields=["username"])

        log_security_event(
            user=request.user,
            action=SecurityAuditLog.Action.USERNAME_CHANGED,
            request=request,
            details=f"Username updated from '{old_username}' to '{new_username}'"
        )
        messages.success(request, f"Username updated successfully to '{new_username}'. Please use your new username on your next login.")
        return redirect("account_settings")

    context = {
        "username_form": username_form,
        "password_form": password_form,
        "active_sessions_count": active_sessions_count,
    }
    return render(request, "leads/my_account.html", context)


@login_required
@require_POST
def change_password_view(request):
    """
    Updates the authenticated MD/Admin password securely using Django password hashing.
    Invalidates session and requires re-login after password change.
    """
    if not request.user.is_md:
        return HttpResponseForbidden("Access denied. Only MD/Admin can change password.")

    password_form = ChangePasswordForm(user=request.user, data=request.POST)
    username_form = ChangeUsernameForm(user=request.user)
    active_sessions_count = _get_active_user_sessions_count(request.user)

    if password_form.is_valid():
        user = request.user
        new_password = password_form.cleaned_data["new_password"]
        user.set_password(new_password)
        user.save()

        log_security_event(
            user=user,
            action=SecurityAuditLog.Action.PASSWORD_CHANGED,
            request=request,
            details="Password changed successfully"
        )
        logout(request)
        messages.success(request, "Password changed successfully. For maximum security, please log in again with your new password.")
        return redirect("login")

    context = {
        "username_form": username_form,
        "password_form": password_form,
        "active_sessions_count": active_sessions_count,
    }
    return render(request, "leads/my_account.html", context)


@login_required
@require_POST
def logout_other_sessions_view(request):
    """
    Invalidates all other active sessions for the authenticated user across devices.
    """
    if not request.user.is_md:
        return HttpResponseForbidden("Access denied. Only MD/Admin can manage active sessions.")

    current_session_key = request.session.session_key
    logged_out_count = _logout_other_user_sessions(request.user, current_session_key)

    log_security_event(
        user=request.user,
        action=SecurityAuditLog.Action.OTHER_SESSIONS_LOGGED_OUT,
        request=request,
        details=f"Invalidated {logged_out_count} other active session(s)"
    )
    messages.success(request, f"Logged out from {logged_out_count} other device session(s) successfully.")
    return redirect("account_settings")


@login_required
def security_audit_log_view(request):
    """
    Renders the security audit log trail for MD/Admin.
    """
    if not request.user.is_md:
        return HttpResponseForbidden("Access denied. Only MD/Admin can view security audit logs.")

    audit_logs = SecurityAuditLog.objects.filter(user=request.user).order_by("-timestamp")
    context = {
        "audit_logs": audit_logs
    }
    return render(request, "leads/security_audit_log.html", context)


