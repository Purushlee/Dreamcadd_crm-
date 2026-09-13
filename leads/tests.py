from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from leads.models import (
    CallLog, CompanySettings, Course, Followup, ImportBatch, Lead, LeadActivity,
    TelecallerProfile, User, WhatsAppTemplate
)
from leads.services import assign_lead_round_robin, normalize_phone, process_incoming_whatsapp


class DreamCaddCoreTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        
        # MD User
        self.md_user = User.objects.create_user(
            username="test_md", password="mdpassword", role=User.Role.MD, is_superuser=True
        )

        # Telecallers
        self.telecaller1 = User.objects.create_user(
            username="caller1", password="callerpassword", role=User.Role.TELECALLER
        )
        self.telecaller1.telecaller_profile.max_leads = 10
        self.telecaller1.telecaller_profile.save()

        self.telecaller2 = User.objects.create_user(
            username="caller2", password="callerpassword", role=User.Role.TELECALLER
        )

        # Course
        self.course = Course.objects.create(
            course_name="Revit Architecture", duration="2 Months", fee=15000.00
        )

    def test_singleton_company_settings(self):
        """Test that CompanySettings enforces a single-record rule."""
        c1 = CompanySettings.load()
        c1.company_name = "DreamCadd Head Office"
        c1.save()

        c2 = CompanySettings(company_name="Should Not Create Second Record")
        c2.save()

        self.assertEqual(CompanySettings.objects.count(), 1)
        self.assertEqual(CompanySettings.load().company_name, "Should Not Create Second Record")

    def test_whatsapp_template_draft_default(self):
        """Test that new WhatsApp templates default to status DRAFT."""
        tmpl = WhatsAppTemplate.objects.create(
            template_name="test_welcome",
            meta_template_name="test_welcome_meta",
            body_text="Hi {{name}}, welcome to {{company_name}}!"
        )
        self.assertEqual(tmpl.status, WhatsAppTemplate.Status.DRAFT)

    def test_phone_normalization(self):
        """Test actual 10-digit phone formatting without 91 prefix."""
        self.assertEqual(normalize_phone("9876543210"), "9876543210")
        self.assertEqual(normalize_phone("+91 98765 43210"), "9876543210")
        self.assertEqual(normalize_phone("09876543210"), "9876543210")
        self.assertEqual(normalize_phone("919876543210"), "9876543210")

    def test_atomic_round_robin_allocation(self):
        """Test round-robin assignment considering caller capacity."""
        lead1 = Lead.objects.create(name="Student A", phone="9000000001", interested_course=self.course)
        lead2 = Lead.objects.create(name="Student B", phone="9000000002", interested_course=self.course)

        assigned1 = assign_lead_round_robin(lead1)
        assigned2 = assign_lead_round_robin(lead2)

        self.assertIsNotNone(assigned1)
        self.assertIsNotNone(assigned2)
        # Should distribute evenly between caller1 and caller2
        self.assertNotEqual(assigned1, assigned2)

    def test_telecaller_lead_access_isolation(self):
        """Test that a telecaller receives 403 Forbidden accessing another caller's lead."""
        lead = Lead.objects.create(name="Private Student", phone="9000000003", assigned_to=self.telecaller1)
        
        self.client.login(username="caller2", password="callerpassword")
        response = self.client.get(reverse("lead_detail", kwargs={"lead_id": lead.id}))
        self.assertEqual(response.status_code, 403)

    def test_n8n_api_key_security(self):
        """Test that n8n REST endpoints reject unauthenticated requests and accept valid X-API-KEY."""
        from django.conf import settings
        url = reverse("n8n_course_lookup")
        
        # Unauthorized without key
        resp_unauth = self.client.get(url)
        self.assertEqual(resp_unauth.status_code, 401)

        # Authorized with key
        resp_auth = self.client.get(url, HTTP_X_API_KEY=settings.N8N_API_KEY)
        self.assertEqual(resp_auth.status_code, 200)
        self.assertIn("courses", resp_auth.json())

    def test_incoming_whatsapp_hot_lead_intent(self):
        """Test that incoming student reply with 'fees' updates priority to HOT and score +25."""
        lead = Lead.objects.create(name="Interested Student", phone="9888888888", priority=Lead.Priority.COLD, lead_score=50)
        
        result = process_incoming_whatsapp("9888888888", "What are the fees for Revit?")
        
        lead.refresh_from_db()
        self.assertTrue(result["is_hot"])
        self.assertEqual(lead.priority, Lead.Priority.HOT)
        self.assertEqual(lead.lead_score, 75)
        self.assertIn("fee", result["auto_reply"])

    def test_quick_call_log_api(self):
        """Test mobile quick call log API endpoint for one-tap response and follow-up auto-save."""
        lead = Lead.objects.create(name="Quick Call Lead", phone="919999988888", assigned_to=self.telecaller1)
        self.client.login(username="caller1", password="callerpassword")

        url = reverse("api_quick_call_log", kwargs={"lead_id": lead.id})
        payload = {
            "outcome": "INTERESTED",
            "notes": "Student requested course curriculum PDF",
            "followup_date": "2026-09-20",
            "followup_time": "14:30",
            "followup_note": "Call after lunch"
        }

        resp = self.client.post(url, data=payload, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")

        # Verify DB updates
        lead.refresh_from_db()
        self.assertEqual(lead.status, Lead.Status.INTERESTED)
        self.assertTrue(CallLog.objects.filter(lead=lead, caller=self.telecaller1, call_result=CallLog.Result.INTERESTED).exists())
        self.assertTrue(Followup.objects.filter(lead=lead, caller=self.telecaller1).exists())

    def test_unassign_leads_and_export_history(self):
        """Test MD unassigning assigned leads back to unallocated pool and CSV export audit logging."""
        lead1 = Lead.objects.create(name="Assigned Lead 1", phone="919111111111", assigned_to=self.telecaller1)
        lead2 = Lead.objects.create(name="Assigned Lead 2", phone="919222222222", assigned_to=self.telecaller1)

        self.client.login(username="test_md", password="mdpassword")

        # Unassign leads
        unassign_url = reverse("unassign_leads")
        unassign_resp = self.client.post(unassign_url, {"lead_ids": [lead1.id, lead2.id]})
        self.assertEqual(unassign_resp.status_code, 302)

        lead1.refresh_from_db()
        lead2.refresh_from_db()
        self.assertIsNone(lead1.assigned_to)
        self.assertIsNone(lead2.assigned_to)

        # Export CSV
        export_url = reverse("export_leads_csv")
        export_resp = self.client.get(export_url)
        self.assertEqual(export_resp.status_code, 200)

        # Verify ExportHistory record was logged
        from leads.models import ExportHistory
        self.assertTrue(ExportHistory.objects.filter(user=self.md_user).exists())

    def test_idempotent_call_session_log(self):
        """Test that duplicate call_session_id submissions return success without duplicate CallLogs."""
        lead = Lead.objects.create(name="Idempotent Test Lead", phone="9988776655", assigned_to=self.telecaller1)
        self.client.login(username="caller1", password="callerpassword")

        url = reverse("api_quick_call_log", kwargs={"lead_id": lead.id})
        session_id = "CS-TEST-SESSION-999"
        payload = {
            "outcome": "INTERESTED",
            "notes": "First submission",
            "call_session_id": session_id
        }

        # First POST
        resp1 = self.client.post(url, data=payload, content_type="application/json")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(CallLog.objects.filter(call_session_id=session_id).count(), 1)

        # Duplicate POST with same call_session_id
        payload["notes"] = "Duplicate submission attempt"
        resp2 = self.client.post(url, data=payload, content_type="application/json")
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(resp2.json().get("idempotent", False))
        
        # Verify call count remains 1 and NO duplicate CallLog was created
        self.assertEqual(CallLog.objects.filter(call_session_id=session_id).count(), 1)

    def test_bulk_allocate_view(self):
        """Test GET /md/bulk-allocate/ renders cleanly without NameError."""
        self.client.login(username="test_md", password="mdpassword")
        response = self.client.get(reverse("bulk_allocate"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("unassigned_total", response.context)

    def test_all_md_views_render_cleanly(self):
        """Test that all MD control views render cleanly without template errors or NoReverseMatch."""
        self.client.login(username="test_md", password="mdpassword")
        md_urls = [
            reverse("md_dashboard"),
            reverse("manage_callers"),
            reverse("bulk_allocate"),
            reverse("company_settings"),
            reverse("manage_courses"),
            reverse("whatsapp_templates"),
            reverse("import_history"),
            reverse("allocation_history"),
            reverse("export_history"),
            reverse("telecaller_report", kwargs={"caller_id": self.telecaller1.id}),
        ]
        for u in md_urls:
            resp = self.client.get(u)
            self.assertEqual(resp.status_code, 200, f"Failed rendering view at {u}")

    def test_unique_customer_contact_counting_vs_call_attempts(self):
        """
        Verify that calling the same customer multiple times increments Call Attempts (CallLogs)
        while keeping Unique Customers Contacted strictly equal to 1.
        """
        balaji = Lead.objects.create(name="Balaji", phone="9876543120", assigned_to=self.telecaller1)
        self.client.login(username="caller1", password="callerpassword")

        url = reverse("api_quick_call_log", kwargs={"lead_id": balaji.id})

        # Initial state
        self.assertFalse(balaji.contacted)
        self.assertEqual(balaji.attempts_count, 0)

        # Call 1: NOT_ANSWERED
        r1 = self.client.post(url, data={"outcome": "NOT_ANSWERED", "call_session_id": "CS-101"}, content_type="application/json")
        self.assertEqual(r1.status_code, 200)

        balaji.refresh_from_db()
        self.assertTrue(balaji.contacted)
        self.assertIsNotNone(balaji.first_contacted_at)
        self.assertEqual(balaji.attempts_count, 1)

        # Call 2: INTERESTED
        r2 = self.client.post(url, data={"outcome": "INTERESTED", "call_session_id": "CS-102"}, content_type="application/json")
        self.assertEqual(r2.status_code, 200)

        balaji.refresh_from_db()
        self.assertTrue(balaji.contacted)
        self.assertEqual(balaji.attempts_count, 2)

        # Call 3: CALL_BACK
        r3 = self.client.post(url, data={"outcome": "CALL_BACK", "call_session_id": "CS-103"}, content_type="application/json")
        self.assertEqual(r3.status_code, 200)

        balaji.refresh_from_db()
        self.assertEqual(balaji.attempts_count, 3)

        # Dashboard metrics verification
        tc_leads = Lead.objects.filter(assigned_to=self.telecaller1)
        unique_contacted = tc_leads.filter(contacted=True).count()
        total_attempts = CallLog.objects.filter(caller=self.telecaller1).count()

        self.assertEqual(unique_contacted, 1)
        self.assertEqual(total_attempts, 3)

    def test_edit_response_api(self):
        """
        Verify that editing a customer response updates the response/status without altering unique customer count.
        """
        balaji = Lead.objects.create(name="Balaji Edit", phone="9876543121", assigned_to=self.telecaller1, contacted=True)
        self.client.login(username="caller1", password="callerpassword")

        url = reverse("api_edit_response", kwargs={"lead_id": balaji.id})
        payload = {
            "new_response": "INTERESTED",
            "notes": "Customer called back to enquire about course"
        }

        resp = self.client.post(url, data=payload, content_type="application/json")
        self.assertEqual(resp.status_code, 200)

        balaji.refresh_from_db()
        self.assertEqual(balaji.status, Lead.Status.INTERESTED)
        self.assertEqual(balaji.current_response, "Interested 🟢")

        # Verify activity was logged
        self.assertTrue(LeadActivity.objects.filter(lead=balaji, activity_type="RESPONSE_UPDATED").exists())

        # Verify unique count remains 1
        self.assertEqual(Lead.objects.filter(assigned_to=self.telecaller1, contacted=True).count(), 1)




