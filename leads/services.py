"""
Enhanced Business Logic and Service Layer for DreamCadd Lead Management Application:

  - Excel Import & ImportBatch tracking
  - Canonical E.164 Phone Normalization
  - Atomic Concurrency-Safe Round-Robin Lead Allocation
  - Lead Activity Timeline Logging
  - Meta Approved WhatsApp Template Rendering & Cloud API Integration
  - Intelligent Intent Auto-Reply Processing
"""
import re
import logging
import pandas as pd
import requests

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from .models import (
    Branch, CompanySettings, Course, ImportBatch, Lead, LeadActivity,
    LeadAssignment, LeadSource, Notification, User, WhatsAppMessage, WhatsAppTemplate
)

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"name", "phone"}


def normalize_phone(raw_phone) -> str:
    """
    Strips all non-digit characters.
    Returns the actual phone number without '91' country code prefix.
    If 12 digits starting with '91', strips '91' prefix to return the 10-digit number.
    If starts with '0', strips leading zeroes.
    """
    digits = re.sub(r"\D", "", str(raw_phone or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) > 10 and digits.startswith("0"):
        digits = digits.lstrip("0")
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
    return digits


def log_lead_activity(lead: Lead, activity_type: str, description: str, actor: User = None) -> LeadActivity:
    """Helper to append a structured activity entry into the lead's timeline."""
    return LeadActivity.objects.create(
        lead=lead,
        actor=actor,
        activity_type=activity_type,
        description=description,
    )


def import_leads_from_excel(file_obj, batch_label: str, uploaded_by: User = None) -> dict:
    """
    Reads an uploaded Excel (.xlsx, .xls), CSV (.csv), or JSON (.json) file,
    creates an ImportBatch audit record, and upserts Lead rows efficiently.
    """
    summary = {"new": 0, "updated": 0, "duplicate_ignored": 0, "errors": []}
    filename = getattr(file_obj, "name", "").lower()

    try:
        if filename.endswith(".json"):
            df = pd.read_json(file_obj)
        elif filename.endswith(".csv"):
            df = pd.read_csv(file_obj)
        else:
            df = pd.read_excel(file_obj)
    except Exception as exc:
        summary["errors"].append(f"Could not read uploaded file: {exc}")
        return summary

    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        summary["errors"].append(f"Missing required column(s): {', '.join(missing)}")
        return summary

    batch_code = f"BATCH-{timezone.now().strftime('%Y%m%d%H%M%S')}-{batch_label[:30]}"
    import_batch = ImportBatch.objects.create(
        batch_code=batch_code,
        file_name=getattr(file_obj, "name", "excel_upload.xlsx"),
        uploaded_by=uploaded_by,
        total_rows=len(df),
    )

    excel_source, _ = LeadSource.objects.get_or_create(name="Excel Upload", defaults={"active": True})

    for idx, row in df.iterrows():
        try:
            name = str(row.get("name", "")).strip()
            raw_phone = row.get("phone", "")
            phone = normalize_phone(raw_phone)

            if not name or not phone or len(phone) < 10:
                summary["errors"].append(f"Row {idx + 2}: missing/invalid name or phone, skipped")
                continue

            email = str(row.get("email", "")).strip() or None
            education = str(row.get("education", "")).strip()
            college = str(row.get("college", "")).strip()
            course_name = str(row.get("course", "")).strip()

            interested_course = None
            if course_name and course_name.lower() != "nan":
                interested_course, _ = Course.objects.get_or_create(
                    course_name__iexact=course_name,
                    defaults={"course_name": course_name},
                )

            existing = Lead.objects.filter(phone=phone).first()

            if existing is None:
                new_lead = Lead.objects.create(
                    name=name,
                    phone=phone,
                    email=email,
                    education=education,
                    college=college,
                    interested_course=interested_course,
                    source_batch=batch_label,
                    import_batch=import_batch,
                    lead_source=excel_source,
                )
                summary["new"] += 1
                log_lead_activity(new_lead, "IMPORTED", f"Imported via Excel batch {batch_label}", actor=uploaded_by)
                continue

            changed = False
            for field, value in [
                ("name", name), ("email", email),
                ("education", education), ("college", college),
            ]:
                if value and getattr(existing, field) != value:
                    setattr(existing, field, value)
                    changed = True

            if interested_course and existing.interested_course_id != interested_course.id:
                existing.interested_course = interested_course
                changed = True

            if changed:
                existing.source_batch = batch_label
                existing.import_batch = import_batch
                existing.save()
                summary["updated"] += 1
                log_lead_activity(existing, "UPDATED", f"Updated details via Excel import {batch_label}", actor=uploaded_by)
            else:
                summary["duplicate_ignored"] += 1

        except Exception as exc:
            summary["errors"].append(f"Row {idx + 2}: {exc}")

    import_batch.new_leads = summary["new"]
    import_batch.updated_leads = summary["updated"]
    import_batch.duplicates = summary["duplicate_ignored"]
    import_batch.errors = summary["errors"]
    import_batch.save()

    return summary


def assign_lead_round_robin(lead: Lead, actor: User = None) -> User | None:
    """
    Assigns a lead to the active telecaller with the lowest current workload within their TelecallerProfile capacity.
    Uses atomic transaction & row-locking (select_for_update) to prevent race conditions during concurrent imports.
    """
    with transaction.atomic():
        active_statuses = [Lead.Status.ASSIGNED, Lead.Status.CONTACTED, Lead.Status.INTERESTED]
        
        telecallers = list(
            User.objects.select_for_update()
            .filter(role=User.Role.TELECALLER, status="ACTIVE")
            .annotate(active_count=Count("assigned_leads", filter=Q(assigned_leads__status__in=active_statuses)))
            .order_by("active_count")
        )

        eligible_caller = None
        for caller in telecallers:
            max_capacity = caller.telecaller_profile.max_leads
            if caller.active_count < max_capacity:
                eligible_caller = caller
                break

        if not eligible_caller:
            logger.warning("No telecaller available or under capacity to assign lead %s", lead.phone)
            return None

        lead.assigned_to = eligible_caller
        lead.status = Lead.Status.ASSIGNED
        lead.save(update_fields=["assigned_to", "status", "updated_at"])

        LeadAssignment.objects.create(lead=lead, caller=eligible_caller)
        log_lead_activity(lead, "ASSIGNED", f"Assigned to telecaller {eligible_caller.get_full_name() or eligible_caller.username}", actor=actor)

        # Notify telecaller
        Notification.objects.create(
            user=eligible_caller,
            title="New Lead Assigned",
            message=f"Lead '{lead.name}' ({lead.phone}) has been assigned to you."
        )

        return eligible_caller


def render_whatsapp_text(raw_text: str, lead: Lead, caller=None) -> str:
    """Replaces dynamic variable placeholders in any WhatsApp text string with actual lead & company details."""
    company = CompanySettings.load()
    if caller and (caller.get_full_name() or caller.username):
        caller_name = caller.get_full_name() or caller.username
    else:
        caller_name = lead.assignee_display
    course_name = lead.interested_course.course_name if lead.interested_course else "our courses"
    branch_name = lead.preferred_branch.branch_name if lead.preferred_branch else "DreamCadd"
    course_fee = f"₹{lead.interested_course.fee}" if (lead.interested_course and lead.interested_course.fee) else "Contact for details"

    replacements = {
        "{{name}}": lead.name,
        "{{course}}": course_name,
        "{{branch}}": branch_name,
        "{{caller_name}}": caller_name,
        "{{company_name}}": company.company_name,
        "{{fee}}": course_fee,
        "{{phone}}": company.main_phone,
    }

    text = raw_text or ""
    for key, val in replacements.items():
        text = text.replace(key, str(val))
    return text


def render_whatsapp_template(template: WhatsAppTemplate, lead: Lead) -> str:
    """Replaces dynamic variable placeholders in template text with actual lead & company details."""
    return render_whatsapp_text(template.body_text, lead)



def send_whatsapp_text(lead: Lead, message: str) -> WhatsAppMessage:
    """Sends outbound text message via Meta WhatsApp Cloud API and records it."""
    record = WhatsAppMessage.objects.create(
        lead=lead, message=message,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        status=WhatsAppMessage.Status.SENT,
    )

    token = getattr(settings, "WHATSAPP_ACCESS_TOKEN", "")
    phone_number_id = getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "")
    if not token or not phone_number_id:
        logger.warning("WhatsApp credentials not configured; message logged but not sent.")
        record.status = WhatsAppMessage.Status.FAILED
        record.save(update_fields=["status"])
        log_lead_activity(lead, "WA_FAILED", f"WhatsApp message failed to send: credentials missing.")
        return record

    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": lead.phone,
        "type": "text",
        "text": {"body": message},
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code >= 300:
            logger.error("WhatsApp send failed: %s", resp.text)
            record.status = WhatsAppMessage.Status.FAILED
            record.save(update_fields=["status"])
            log_lead_activity(lead, "WA_FAILED", f"WhatsApp send failed with status {resp.status_code}")
        else:
            log_lead_activity(lead, "WA_SENT", f"Outbound WhatsApp message sent: {message[:40]}...")
    except requests.RequestException as exc:
        logger.error("WhatsApp send exception: %s", exc)
        record.status = WhatsAppMessage.Status.FAILED
        record.save(update_fields=["status"])

    return record


def send_new_lead_message(lead: Lead):
    """Workflow 2: Greet freshly assigned lead."""
    template = WhatsAppTemplate.objects.filter(template_name="welcome_lead", status=WhatsAppTemplate.Status.APPROVED).first()
    if template:
        message = render_whatsapp_template(template, lead)
    else:
        company = CompanySettings.load()
        course_line = f" We offer the {lead.interested_course.course_name} program." if lead.interested_course else ""
        message = f"Hi {lead.name}, welcome to {company.company_name}!{course_line} Our counselor {lead.assignee_display} will contact you shortly."
    
    return send_whatsapp_text(lead, message)


def process_incoming_whatsapp(phone: str, message_text: str) -> dict:
    """
    Intelligent n8n / webhook incoming reply handler:
    1. Identifies lead via phone
    2. Logs message in database
    3. Analyzes intent for course info, fee, address, syllabus
    4. Generates automated contextual reply
    5. Updates lead status & priority if hot buying intent detected
    """
    normalized = normalize_phone(phone)
    lead = Lead.objects.filter(phone=normalized).first()
    if not lead:
        logger.warning("Incoming WhatsApp from unregistered number: %s", phone)
        return {"status": "unregistered_number", "phone": phone}

    WhatsAppMessage.objects.create(
        lead=lead, message=message_text,
        direction=WhatsAppMessage.Direction.INBOUND,
        status=WhatsAppMessage.Status.RECEIVED,
    )

    log_lead_activity(lead, "WA_REPLIED", f"Student replied via WhatsApp: '{message_text}'")

    if lead.status == Lead.Status.ASSIGNED:
        lead.status = Lead.Status.CONTACTED
        lead.save(update_fields=["status", "updated_at"])

    # Hot Lead Detection
    lower_text = message_text.lower()
    hot_keywords = ["interested", "admission", "fees", "fee", "cost", "join", "enroll", "location", "address", "syllabus"]
    is_hot = any(kw in lower_text for kw in hot_keywords)

    if is_hot:
        lead.priority = Lead.Priority.HOT
        lead.lead_score = min(100, lead.lead_score + 25)
        if lead.status in [Lead.Status.NEW, Lead.Status.ASSIGNED, Lead.Status.CONTACTED]:
            lead.status = Lead.Status.INTERESTED
        lead.save(update_fields=["priority", "lead_score", "status", "updated_at"])

        if lead.assigned_to:
            Notification.objects.create(
                user=lead.assigned_to,
                title="🔥 HOT Lead Alert!",
                message=f"Student {lead.name} replied with strong buying interest: '{message_text[:60]}'"
            )

    # AI Lead Intelligence & Sales Assistant Engine
    company = CompanySettings.load()
    course = lead.interested_course
    course_title = course.course_name if course else "CAD/BIM Training"

    if "fee" in lower_text or "fees" in lower_text or "cost" in lower_text:
        lead.ai_intent = "Fee & Payment Enquiry"
        lead.ai_next_action = f"Call student immediately to explain {course_title} fee structure & EMI options."
        lead.ai_suggested_response = f"Hi {lead.name}, I noticed you were enquiring about the {course_title} fees. We have flexible payment options available!"
        lead.ai_summary = f"Student asked about course fees for {course_title}. High buying intent."
    elif "syllabus" in lower_text or "details" in lower_text:
        lead.ai_intent = "Syllabus & Module Enquiry"
        lead.ai_next_action = f"Send {course_title} syllabus PDF via WhatsApp and schedule callback."
        lead.ai_suggested_response = f"Hi {lead.name}, I've sent the complete {course_title} syllabus for your review. Would you have 5 mins for a quick discussion?"
        lead.ai_summary = f"Student requested detailed course syllabus for {course_title}."
    elif "address" in lower_text or "location" in lower_text or "branch" in lower_text:
        lead.ai_intent = "Branch Location & Walk-in Enquiry"
        lead.ai_next_action = "Invite student for a campus visit & demo class at the nearest branch."
        lead.ai_suggested_response = f"Hi {lead.name}, our training center is located at {company.address}. When would you like to drop by for a free demo class?"
        lead.ai_summary = "Student interested in visiting the campus branch."
    elif is_hot:
        lead.ai_intent = "High-Interest Buying Signal"
        lead.ai_next_action = "HOT LEAD! Call immediately to confirm admission process."
        lead.ai_suggested_response = f"Hi {lead.name}, thank you for your interest! I can help reserve your seat for the upcoming {course_title} batch."
        lead.ai_summary = f"Student expressed strong buying interest: '{message_text}'"
    else:
        lead.ai_intent = "General Student Enquiry"
        lead.ai_next_action = "Contact student to understand learning goals & background."
        lead.ai_suggested_response = f"Hi {lead.name}, I'm calling from {company.company_name} regarding your enquiry for {course_title}."
        lead.ai_summary = f"Student messaged: '{message_text[:80]}'"

    lead.save(update_fields=["ai_intent", "ai_next_action", "ai_suggested_response", "ai_summary", "updated_at"])

    # Dynamic Intent Response Generation
    auto_reply = ""
    if "fee" in lower_text or "fees" in lower_text or "cost" in lower_text:
        fee_info = f"₹{course.fee}" if course and course.fee else "affordable rates depending on module"
        auto_reply = f"Hi {lead.name}, the course fee for {course_title} is {fee_info}. Duration: {course.duration if course else 'Flexible'}."
    elif "address" in lower_text or "location" in lower_text or "branch" in lower_text:
        branches = Branch.objects.filter(status="ACTIVE")
        branch_str = ", ".join([f"{b.branch_name} ({b.location})" for b in branches]) or company.address
        auto_reply = f"Our branches are located at: {branch_str}. Main Head Office: {company.address}."
    elif "syllabus" in lower_text or "details" in lower_text:
        desc = course.description if course and course.description else "Hands-on CAD/BIM industrial training."
        auto_reply = f"Details for {course_title}: {desc}"

    return {
        "status": "success",
        "lead_id": lead.id,
        "lead_name": lead.name,
        "is_hot": is_hot,
        "ai_intent": lead.ai_intent,
        "ai_next_action": lead.ai_next_action,
        "auto_reply": auto_reply,
        "assignee": lead.assignee_display,
    }
