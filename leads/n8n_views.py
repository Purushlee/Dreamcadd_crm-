import json
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .api_security import require_n8n_api_key, verify_meta_webhook_signature
from .models import Branch, CompanySettings, Course, Followup, Lead, LeadActivity, Notification, WhatsAppTemplate
from .services import normalize_phone, process_incoming_whatsapp, render_whatsapp_template, send_whatsapp_text


@csrf_exempt
@require_http_methods(["GET"])
@require_n8n_api_key
def get_pending_followups(request):
    """
    n8n Endpoint: Returns pending follow-ups due up to current time or today for automated cron reminder dispatches.
    """
    now = timezone.now()
    pending = Followup.objects.filter(
        status=Followup.Status.PENDING,
        scheduled_date__lte=now + timezone.timedelta(minutes=30)
    ).select_related("lead", "caller")

    results = []
    for f in pending:
        results.append({
            "followup_id": f.id,
            "scheduled_date": f.scheduled_date.isoformat(),
            "lead_id": f.lead.id,
            "lead_name": f.lead.name,
            "lead_phone": f.lead.phone,
            "caller_username": f.caller.username,
            "caller_phone": f.caller.phone or "",
            "remarks": f.remarks,
        })

    return JsonResponse({"count": len(results), "followups": results})


@csrf_exempt
@require_http_methods(["POST"])
@require_n8n_api_key
def handle_n8n_incoming_whatsapp(request):
    """
    n8n Webhook Endpoint: Receives WhatsApp student reply payload from n8n/Meta.
    Verifies Meta HMAC signature & n8n API key, performs intent detection, auto-replies, and triggers hot alerts.
    """
    if not verify_meta_webhook_signature(request):
        return JsonResponse({"error": "Forbidden", "detail": "Invalid Meta HMAC SHA-256 signature."}, status=403)

    try:
        data = json.loads(request.body.decode("utf-8"))
        phone = data.get("phone") or data.get("from")
        text = data.get("message") or data.get("text") or data.get("body", "")

        if not phone or not text:
            return JsonResponse({"error": "Bad Request", "detail": "'phone' and 'message' fields required."}, status=400)

        result = process_incoming_whatsapp(phone, text)
        return JsonResponse(result)

    except Exception as exc:
        return JsonResponse({"error": "Internal Error", "detail": str(exc)}, status=500)


@csrf_exempt
@require_http_methods(["GET"])
@require_n8n_api_key
def get_course_lookup(request):
    """
    n8n Endpoint: Returns list of active courses, fees, and syllabus URLs.
    """
    courses = Course.objects.filter(status="ACTIVE").select_related("category")
    results = []
    for c in courses:
        results.append({
            "id": c.id,
            "course_name": c.course_name,
            "category": c.category.name if c.category else "General",
            "duration": c.duration,
            "fee": str(c.fee) if c.fee else None,
            "description": c.description,
            "eligibility": c.eligibility,
            "syllabus_url": request.build_absolute_uri(c.syllabus_file.url) if c.syllabus_file else None,
        })
    return JsonResponse({"courses": results})


@csrf_exempt
@require_http_methods(["GET"])
@require_n8n_api_key
def get_company_info(request):
    """
    n8n Endpoint: Returns Company master configuration and active branch locations.
    """
    company = CompanySettings.load()
    branches = Branch.objects.filter(status="ACTIVE")
    
    branch_data = []
    for b in branches:
        branch_data.append({
            "name": b.branch_name,
            "location": b.location,
            "address": b.address,
            "phone": b.phone,
        })

    return JsonResponse({
        "company_name": company.company_name,
        "website": company.website,
        "main_phone": company.main_phone,
        "email": company.email,
        "address": company.address,
        "branches": branch_data,
    })


@csrf_exempt
@require_http_methods(["POST"])
@require_n8n_api_key
def trigger_hot_alert(request):
    """
    n8n Endpoint: Manually trigger a hot lead alert to telecaller.
    """
    try:
        data = json.loads(request.body.decode("utf-8"))
        lead_id = data.get("lead_id")
        lead = Lead.objects.get(id=lead_id)
        
        lead.priority = Lead.Priority.HOT
        lead.save(update_fields=["priority", "updated_at"])

        if lead.assigned_to:
            Notification.objects.create(
                user=lead.assigned_to,
                title="🔥 n8n HOT Lead Alert!",
                message=data.get("message", f"Lead {lead.name} requires immediate callback!")
            )
            return JsonResponse({"status": "alert_created", "lead": lead.name, "assigned_to": lead.assigned_to.username})
        
        return JsonResponse({"status": "no_assignee", "lead": lead.name})
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@csrf_exempt
@require_http_methods(["GET"])
@require_n8n_api_key
def find_lead(request):
    """
    n8n Endpoint: Find lead by phone number for Workflow 2 (Student Reply Intent).
    Accepts phone as query param: /api/n8n/find-lead/?phone=9876543210
    Returns lead details including assigned telecaller, course, and current status.
    """
    raw_phone = request.GET.get("phone", "").strip()
    if not raw_phone:
        return JsonResponse({"error": "Bad Request", "detail": "'phone' query parameter is required."}, status=400)

    normalized = normalize_phone(raw_phone)
    lead = Lead.objects.filter(
        Q(phone=normalized) | Q(phone=raw_phone) | Q(phone="91" + normalized)
    ).select_related("interested_course", "assigned_to").first()

    if not lead:
        return JsonResponse({
            "found": False,
            "phone": raw_phone,
            "detail": "No lead found with this phone number."
        }, status=404)

    caller_info = None
    if lead.assigned_to:
        caller_info = {
            "id": lead.assigned_to.id,
            "username": lead.assigned_to.username,
            "full_name": lead.assigned_to.get_full_name() or lead.assigned_to.username,
            "phone": lead.assigned_to.phone or "",
        }

    course_info = None
    if lead.interested_course:
        course_info = {
            "id": lead.interested_course.id,
            "name": lead.interested_course.course_name,
            "duration": lead.interested_course.duration,
            "fee": str(lead.interested_course.fee) if lead.interested_course.fee else None,
        }

    return JsonResponse({
        "found": True,
        "lead_id": lead.id,
        "name": lead.name,
        "phone": lead.phone,
        "email": lead.email or "",
        "status": lead.status,
        "priority": lead.priority,
        "lead_score": lead.lead_score,
        "ai_intent": lead.ai_intent,
        "course": course_info,
        "caller": caller_info,
    })


@csrf_exempt
@require_http_methods(["POST"])
@require_n8n_api_key
def update_lead_status(request):
    """
    n8n Endpoint: Update lead status after AI intent detection in Workflow 2.
    Called by n8n after AI node determines intent and action.

    Expected payload:
    {
      "lead_id": 1,
      "status": "INTERESTED",          # optional
      "ai_intent": "FEE_ENQUIRY",       # optional
      "ai_next_action": "...",           # optional
      "ai_summary": "...",               # optional
      "note": "Human-readable log msg"   # optional — logged in LeadActivity
    }
    """
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Bad Request", "detail": "Invalid JSON payload."}, status=400)

    lead_id = data.get("lead_id")
    if not lead_id:
        return JsonResponse({"error": "Bad Request", "detail": "'lead_id' is required."}, status=400)

    try:
        lead = Lead.objects.get(id=lead_id)
    except Lead.DoesNotExist:
        return JsonResponse({"error": "Not Found", "detail": f"Lead with id {lead_id} not found."}, status=404)

    update_fields = ["updated_at"]

    # Status update (validated against Lead.Status choices)
    new_status = data.get("status")
    valid_statuses = [s.value for s in Lead.Status]
    if new_status and new_status in valid_statuses:
        lead.status = new_status
        update_fields.append("status")

    # AI fields
    if data.get("ai_intent"):
        lead.ai_intent = data["ai_intent"]
        update_fields.append("ai_intent")

    if data.get("ai_next_action"):
        lead.ai_next_action = data["ai_next_action"]
        update_fields.append("ai_next_action")

    if data.get("ai_summary"):
        lead.ai_summary = data["ai_summary"]
        update_fields.append("ai_summary")

    lead.save(update_fields=update_fields)

    # Log activity
    note = data.get("note") or f"n8n updated lead status to '{lead.status}' with intent '{lead.ai_intent}'."
    LeadActivity.objects.create(
        lead=lead,
        activity_type="N8N_UPDATE",
        description=note,
    )

    return JsonResponse({
        "status": "updated",
        "lead_id": lead.id,
        "lead_name": lead.name,
        "new_status": lead.status,
        "ai_intent": lead.ai_intent,
    })
