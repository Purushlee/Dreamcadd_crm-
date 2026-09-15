from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from leads.models import (
    CallLog, CompanySettings, Course, Followup, ImportBatch, Lead, LeadActivity,
    TelecallerProfile, User, WhatsAppMessage, WhatsAppTemplate
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

    def test_whatsapp_send_permission_control(self):
        """
        Verify security rule:
        Telecaller 1 can send WhatsApp to their assigned lead (Kamesh),
        but Telecaller 2 gets HTTP 403 Forbidden when trying to send WhatsApp to Kamesh.
        """
        kamesh = Lead.objects.create(name="Kamesh", phone="7092929658", assigned_to=self.telecaller1)

        # Login as Telecaller 2 (unassigned owner)
        self.client.login(username="caller2", password="callerpassword")
        unauth_resp = self.client.post(
            reverse("api_send_whatsapp", kwargs={"lead_id": kamesh.id}),
            data={"message": "Unauthorized message"},
            content_type="application/json"
        )
        self.assertEqual(unauth_resp.status_code, 403)
        self.assertIn("Access Denied", unauth_resp.json()["message"])

        # Login as Telecaller 1 (assigned owner)
        self.client.login(username="caller1", password="callerpassword")
        auth_resp = self.client.post(
            reverse("api_send_whatsapp", kwargs={"lead_id": kamesh.id}),
            data={"message": "Hello Kamesh, here are course details!"},
            content_type="application/json"
        )
        self.assertEqual(auth_resp.status_code, 200)
        self.assertEqual(auth_resp.json()["status"], "success")
        self.assertEqual(WhatsAppMessage.objects.filter(lead=kamesh).count(), 1)

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


class SecurityAccountManagementTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.md_user = User.objects.create_user(
            username="md_sec_user", password="StrongPassword123!", role=User.Role.MD, is_superuser=True
        )
        self.telecaller = User.objects.create_user(
            username="caller_sec_user", password="CallerPassword123!", role=User.Role.TELECALLER
        )

    def test_account_settings_access_control(self):
        """Test that MD can access My Account, but Telecallers receive 403 Forbidden."""
        url = reverse("account_settings")

        # Telecaller -> 403 Forbidden
        self.client.login(username="caller_sec_user", password="CallerPassword123!")
        resp_tele = self.client.get(url)
        self.assertEqual(resp_tele.status_code, 403)

        # MD -> 200 OK
        self.client.login(username="md_sec_user", password="StrongPassword123!")
        resp_md = self.client.get(url)
        self.assertEqual(resp_md.status_code, 200)
        self.assertIn("username_form", resp_md.context)

    def test_change_username_success(self):
        """Test changing username retains account data and creates SecurityAuditLog."""
        from leads.models import SecurityAuditLog
        self.client.login(username="md_sec_user", password="StrongPassword123!")

        url = reverse("change_username")

        # Wrong current password -> fail
        resp_wrong_pass = self.client.post(url, {
            "current_username": "md_sec_user",
            "current_password": "WrongPassword!",
            "new_username": "md_renamed_user"
        })
        self.assertEqual(resp_wrong_pass.status_code, 200)

        # Wrong current username -> fail
        resp_wrong_user = self.client.post(url, {
            "current_username": "wrong_username",
            "current_password": "StrongPassword123!",
            "new_username": "md_renamed_user"
        })
        self.assertEqual(resp_wrong_user.status_code, 200)

        # Valid payload -> success
        resp = self.client.post(url, {
            "current_username": "md_sec_user",
            "current_password": "StrongPassword123!",
            "new_username": "md_renamed_user"
        })
        self.assertEqual(resp.status_code, 302)

        self.md_user.refresh_from_db()
        self.assertEqual(self.md_user.username, "md_renamed_user")
        self.assertEqual(self.md_user.role, User.Role.MD)

        # Audit log verification
        audit_log = SecurityAuditLog.objects.filter(user=self.md_user, action=SecurityAuditLog.Action.USERNAME_CHANGED).first()
        self.assertIsNotNone(audit_log)
        self.assertIn("md_renamed_user", audit_log.details)

    def test_change_username_duplicate_rejected(self):
        """Test that attempting to rename to an existing username fails."""
        self.client.login(username="md_sec_user", password="StrongPassword123!")

        url = reverse("change_username")
        resp = self.client.post(url, {
            "current_username": "md_sec_user",
            "current_password": "StrongPassword123!",
            "new_username": "caller_sec_user"
        })
        self.assertEqual(resp.status_code, 200) # Form invalid, re-renders form

        self.md_user.refresh_from_db()
        self.assertEqual(self.md_user.username, "md_sec_user")

    def test_change_password_complexity_and_logout(self):
        """Test password change enforces current credentials verification, complexity rules, hashes password, and logs user out."""
        from leads.models import SecurityAuditLog
        self.client.login(username="md_sec_user", password="StrongPassword123!")

        url = reverse("change_password")

        # Wrong current username -> fail
        resp_wrong_user = self.client.post(url, {
            "current_username": "wrong_username",
            "current_password": "StrongPassword123!",
            "new_password": "NewStrongPass999!",
            "confirm_password": "NewStrongPass999!"
        })
        self.assertEqual(resp_wrong_user.status_code, 200)

        # Weak password (no special char, no uppercase) -> fail
        resp_weak = self.client.post(url, {
            "current_username": "md_sec_user",
            "current_password": "StrongPassword123!",
            "new_password": "simple",
            "confirm_password": "simple"
        })
        self.assertEqual(resp_weak.status_code, 200)

        # Valid strong password -> success & logout
        resp_strong = self.client.post(url, {
            "current_username": "md_sec_user",
            "current_password": "StrongPassword123!",
            "new_password": "NewStrongPass999!",
            "confirm_password": "NewStrongPass999!"
        })
        self.assertEqual(resp_strong.status_code, 302)

        self.md_user.refresh_from_db()
        self.assertTrue(self.md_user.check_password("NewStrongPass999!"))

        # Audit log verification
        self.assertTrue(SecurityAuditLog.objects.filter(user=self.md_user, action=SecurityAuditLog.Action.PASSWORD_CHANGED).exists())

    def test_logout_other_sessions(self):
        """Test logout other sessions invalidates extra active sessions for the user."""
        from django.contrib.sessions.models import Session
        from leads.models import SecurityAuditLog

        self.client.login(username="md_sec_user", password="StrongPassword123!")
        current_session_key = self.client.session.session_key

        url = reverse("logout_other_sessions")
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)

        self.assertTrue(SecurityAuditLog.objects.filter(user=self.md_user, action=SecurityAuditLog.Action.OTHER_SESSIONS_LOGGED_OUT).exists())

    def test_security_audit_log_view(self):
        """Test security audit log page renders for MD user."""
        self.client.login(username="md_sec_user", password="StrongPassword123!")
        url = reverse("security_audit_log")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("audit_logs", resp.context)

    def test_admin_update_telecaller_credentials(self):
        """Test Admin/MD updating a telecaller's username and password from manage_callers."""
        self.client.login(username="md_sec_user", password="StrongPassword123!")
        url = reverse("manage_callers")

        payload = {
            "update_caller_credentials": "1",
            "caller_id": self.telecaller.id,
            "new_username": "caller_renamed",
            "new_password": "NewCallerPassword123!"
        }
        resp = self.client.post(url, payload)
        self.assertEqual(resp.status_code, 302)

        self.telecaller.refresh_from_db()
        self.assertEqual(self.telecaller.username, "caller_renamed")
        self.assertTrue(self.telecaller.check_password("NewCallerPassword123!"))

    def test_telecaller_filters_and_not_answered_response_transition(self):
        """Test telecaller dashboard context filter counts and Not Answered status transitions."""
        l1 = Lead.objects.create(name="Lead Hot", phone="9111111111", assigned_to=self.telecaller, priority=Lead.Priority.HOT, status=Lead.Status.NEW)
        l2 = Lead.objects.create(name="Lead Not Answered", phone="9222222222", assigned_to=self.telecaller, status=Lead.Status.NOT_CONNECTED, contacted=True)
        l3 = Lead.objects.create(name="Lead Interested", phone="9333333333", assigned_to=self.telecaller, status=Lead.Status.INTERESTED, contacted=True)

        Followup.objects.create(lead=l3, caller=self.telecaller, scheduled_date=timezone.now() + timezone.timedelta(days=1))

        self.client.login(username="caller_sec_user", password="CallerPassword123!")
        url = reverse("telecaller_dashboard")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        # Context filter counts assertion
        self.assertEqual(resp.context["hot_count"], 1)
        self.assertEqual(resp.context["pending_leads_count"], 1)
        self.assertEqual(resp.context["interested_count"], 1)
        self.assertEqual(resp.context["followup_count"], 1)
        self.assertEqual(resp.context["not_answered_count"], 1)

        # Edit Not Answered lead to INTERESTED
        edit_url = reverse("api_edit_response", kwargs={"lead_id": l2.id})
        edit_resp = self.client.post(edit_url, data={"new_response": "INTERESTED", "notes": "Customer called back"}, content_type="application/json")
        self.assertEqual(edit_resp.status_code, 200)

        l2.refresh_from_db()
        self.assertEqual(l2.status, Lead.Status.INTERESTED)
        self.assertEqual(l2.current_response, "Interested 🟢")
        self.assertTrue(l2.contacted)

        # Re-query dashboard to verify updated counts
        resp_updated = self.client.get(url)
        self.assertEqual(resp_updated.context["interested_count"], 2)
        self.assertEqual(resp_updated.context["not_answered_count"], 0)

    def test_unique_contacted_count_vs_total_call_attempts(self):
        """
        Verify the exact metric rule:
        5 call attempts to 1 customer (e.g. Kamesh) = 5 CallLogs (5 Call Attempts) but Unique Customers Contacted = 1.
        """
        lead = Lead.objects.create(name="Kamesh", phone="7092929658", assigned_to=self.telecaller)
        self.client.login(username="caller_sec_user", password="CallerPassword123!")

        outcomes = ["NOT_ANSWERED", "BUSY", "NOT_ANSWERED", "CONNECTED", "INTERESTED"]
        for idx, outcome in enumerate(outcomes):
            self.client.post(
                reverse("api_quick_call_log", kwargs={"lead_id": lead.id}),
                content_type="application/json",
                data={"outcome": outcome, "call_duration": 10, "call_session_id": f"SESS-{idx}"}
            )

        lead.refresh_from_db()
        self.assertEqual(CallLog.objects.filter(lead=lead).count(), 5)
        self.assertTrue(lead.contacted)

        resp = self.client.get(reverse("telecaller_dashboard"))
        # 1 unique customer contacted despite 5 call attempts
        self.assertEqual(resp.context["unique_contacted_count"], 1)
        self.assertEqual(resp.context["total_call_attempts"], 5)


class DreamCaddAuthRoleRoutingTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        # ADMIN role user (represents MD)
        self.admin_user = User.objects.create_user(
            username="admin_md", password="AdminPassword123!", role=User.Role.ADMIN
        )
        # MD role user
        self.md_user = User.objects.create_user(
            username="pure_md", password="MdPassword123!", role=User.Role.MD
        )
        # TELECALLER role user
        self.telecaller_user = User.objects.create_user(
            username="telecaller_user", password="CallerPassword123!", role=User.Role.TELECALLER
        )

    def test_admin_role_login_redirects_to_md_dashboard(self):
        """Verify ADMIN role (represented as MD) logs in and redirects to MD/Admin dashboard."""
        login_url = reverse("login")
        resp = self.client.post(login_url, {"username": "admin_md", "password": "AdminPassword123!"})
        self.assertRedirects(resp, reverse("md_dashboard"))

    def test_md_role_login_redirects_to_md_dashboard(self):
        """Verify MD role logs in and redirects to MD/Admin dashboard."""
        login_url = reverse("login")
        resp = self.client.post(login_url, {"username": "pure_md", "password": "MdPassword123!"})
        self.assertRedirects(resp, reverse("md_dashboard"))

    def test_telecaller_role_login_redirects_to_telecaller_dashboard(self):
        """Verify TELECALLER role logs in and redirects strictly to Telecaller dashboard."""
        login_url = reverse("login")
        resp = self.client.post(login_url, {"username": "telecaller_user", "password": "CallerPassword123!"})
        self.assertRedirects(resp, reverse("telecaller_dashboard"))

    def test_invalid_login_credentials_stays_on_login_page_with_error(self):
        """Verify invalid username/password stays on login page with HTTP 200 and error message."""
        login_url = reverse("login")
        resp = self.client.post(login_url, {"username": "admin_md", "password": "WrongPassword!"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Invalid username or password")

    def test_unauthenticated_user_access_redirects_to_login(self):
        """Verify unauthenticated access to MD or Telecaller pages redirects to login."""
        resp_md = self.client.get(reverse("md_dashboard"))
        self.assertEqual(resp_md.status_code, 302)
        self.assertIn(reverse("login"), resp_md.url)

        resp_tc = self.client.get(reverse("telecaller_dashboard"))
        self.assertEqual(resp_tc.status_code, 302)
        self.assertIn(reverse("login"), resp_tc.url)

    def test_admin_cannot_access_telecaller_only_dashboard(self):
        """Verify ADMIN role user cannot access telecaller-only dashboard."""
        self.client.login(username="admin_md", password="AdminPassword123!")
        resp = self.client.get(reverse("telecaller_dashboard"))
        # Should be redirected back to login or forbidden
        self.assertNotEqual(resp.status_code, 200)

    def test_telecaller_cannot_access_md_dashboard(self):
        """Verify TELECALLER role user cannot access MD/Admin dashboard."""
        self.client.login(username="telecaller_user", password="CallerPassword123!")
        resp = self.client.get(reverse("md_dashboard"))
        self.assertNotEqual(resp.status_code, 200)

    def test_authenticated_admin_session_check(self):
        """Verify /api/session-check/ returns HTTP 200 and authenticated=True for active ADMIN session."""
        self.client.login(username="admin_md", password="AdminPassword123!")
        resp = self.client.get(reverse("api_session_check"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["username"], "admin_md")
        self.assertEqual(data["role"], "ADMIN")

    def test_inactive_user_session_check_blocked(self):
        """Verify inactive or disabled user session check is blocked with HTTP 401."""
        inactive_user = User.objects.create_user(
            username="inactive_user", password="InactivePassword123!", role=User.Role.ADMIN, status="INACTIVE"
        )
        self.client.login(username="inactive_user", password="InactivePassword123!")
        resp = self.client.get(reverse("api_session_check"))
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertFalse(data["authenticated"])









