from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """
    Custom user model. Roles:
      MD         - uploads Excel, sees everything, manages telecallers/courses
      TELECALLER - sees only their assigned leads, logs follow-ups
      ADMIN      - full access (django admin / superuser use)
    """
    class Role(models.TextChoices):
        MD = "MD", "MD"
        TELECALLER = "TELECALLER", "Telecaller"
        ADMIN = "ADMIN", "Admin"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.TELECALLER)
    phone = models.CharField(max_length=20, blank=True, null=True)
    status = models.CharField(
        max_length=20,
        choices=[("ACTIVE", "Active"), ("INACTIVE", "Inactive")],
        default="ACTIVE",
    )
    last_activity = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.role})"

    @property
    def is_md(self):
        return self.role == self.Role.MD or self.role == self.Role.ADMIN or self.is_superuser

    @property
    def is_telecaller(self):
        return self.role == self.Role.TELECALLER

    @property
    def telecaller_profile(self):
        profile, _ = TelecallerProfile.objects.get_or_create(user=self)
        return profile

    @property
    def presence_status(self):
        if not self.last_activity:
            return "OFFLINE"
        delta = (timezone.now() - self.last_activity).total_seconds()
        if delta < 300:      # Active in last 5 min
            return "ONLINE"
        elif delta < 900:    # Active 5 - 15 min ago
            return "IDLE"
        return "OFFLINE"

    @property
    def presence_color(self):
        st = self.presence_status
        if st == "ONLINE":
            return "#22C55E"
        elif st == "IDLE":
            return "#F59E0B"
        return "#EF4444"


class UserSessionLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="session_logs")
    login_time = models.DateTimeField(default=timezone.now)
    logout_time = models.DateTimeField(null=True, blank=True)
    last_activity = models.DateTimeField(default=timezone.now)
    ip_address = models.CharField(max_length=45, blank=True)

    class Meta:
        ordering = ["-login_time"]

    def __str__(self):
        return f"Session: {self.user.username} ({self.login_time.strftime('%d-%b %H:%M')})"


class TelecallerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    max_leads = models.IntegerField(default=100)
    daily_call_target = models.IntegerField(default=30)
    specialization = models.ForeignKey("Course", on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"Profile: {self.user.username} (Max: {self.max_leads})"


class CompanySettings(models.Model):
    company_name = models.CharField(max_length=150, default="DreamCadd")
    website = models.URLField(blank=True, default="https://dreamcadd.com")
    main_phone = models.CharField(max_length=20, blank=True, default="+91 9876543210")
    email = models.EmailField(blank=True, default="info@dreamcadd.com")
    address = models.TextField(blank=True, default="123 Tech Park, Anna Salai, Chennai, Tamil Nadu")

    def save(self, *args, **kwargs):
        self.pk = 1  # Enforce Singleton pattern
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return self.company_name


class Branch(models.Model):
    branch_name = models.CharField(max_length=150)
    location = models.CharField(max_length=100)
    address = models.TextField()
    phone = models.CharField(max_length=20)
    status = models.CharField(
        max_length=20,
        choices=[("ACTIVE", "Active"), ("INACTIVE", "Inactive")],
        default="ACTIVE",
    )

    def __str__(self):
        return f"{self.branch_name} ({self.location})"


class LeadSource(models.Model):
    name = models.CharField(max_length=100, unique=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class CourseCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Course(models.Model):
    category = models.ForeignKey(CourseCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="courses")
    course_name = models.CharField(max_length=150)
    duration = models.CharField(max_length=50, blank=True)
    fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    description = models.TextField(blank=True)
    eligibility = models.TextField(blank=True)
    syllabus_file = models.FileField(upload_to="syllabi/", blank=True, null=True)
    status = models.CharField(
        max_length=20,
        choices=[("ACTIVE", "Active"), ("INACTIVE", "Inactive")],
        default="ACTIVE",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.course_name


class ImportBatch(models.Model):
    batch_code = models.CharField(max_length=150, unique=True)
    file_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    total_rows = models.IntegerField(default=0)
    new_leads = models.IntegerField(default=0)
    updated_leads = models.IntegerField(default=0)
    duplicates = models.IntegerField(default=0)
    errors = models.JSONField(default=list)

    def __str__(self):
        return f"{self.batch_code} ({self.uploaded_at.strftime('%d-%b-%Y %H:%M')})"


class AllocationBatch(models.Model):
    batch_code = models.CharField(max_length=150, unique=True)
    import_batch = models.ForeignKey(ImportBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="allocation_batches")
    caller = models.ForeignKey(User, on_delete=models.CASCADE, related_name="allocation_batches")
    allocated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_allocations")
    lead_count = models.IntegerField(default=0)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.batch_code} -> {self.caller.username} ({self.lead_count} leads)"


class Lead(models.Model):
    class Status(models.TextChoices):
        NEW = "NEW", "New"
        ASSIGNED = "ASSIGNED", "Assigned"
        CONTACTED = "CONTACTED", "Contacted"
        INTERESTED = "INTERESTED", "Interested"
        NOT_INTERESTED = "NOT_INTERESTED", "Not Interested"
        CALL_BACK = "CALL_BACK", "Call Back 📞"
        CONVERTED = "CONVERTED", "Converted"
        DROPPED = "DROPPED", "Dropped"
        BUSY = "BUSY", "Busy"
        NOT_ANSWERED = "NOT_ANSWERED", "Not Answered 📵"
        NOT_CONNECTED = "NOT_CONNECTED", "Not Connected"
        SWITCHED_OFF = "SWITCHED_OFF", "Switched Off"
        WRONG_NUMBER = "WRONG_NUMBER", "Wrong Number"
        INVALID_NUMBER = "INVALID_NUMBER", "Invalid Number"
        NO_RESPONSE = "NO_RESPONSE", "No Response"

    class Priority(models.TextChoices):
        HOT = "HOT", "Hot 🔥"
        WARM = "WARM", "Warm 🟡"
        COLD = "COLD", "Cold 🔵"

    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=20, unique=True)  # de-dup key for Excel import
    email = models.EmailField(blank=True, null=True)
    education = models.CharField(max_length=100, blank=True)
    college = models.CharField(max_length=150, blank=True)
    interested_course = models.ForeignKey(
        Course, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads"
    )
    preferred_branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads"
    )
    budget = models.CharField(max_length=50, blank=True)
    requirement_note = models.TextField(blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.NEW)
    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_leads", limit_choices_to={"role": User.Role.TELECALLER},
    )
    assigned_at = models.DateTimeField(blank=True, null=True)
    source_batch = models.CharField(max_length=150, blank=True)
    import_batch = models.ForeignKey(ImportBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads")
    allocation_batch = models.ForeignKey(AllocationBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads")
    lead_source = models.ForeignKey(LeadSource, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads")
    lead_score = models.IntegerField(default=50)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.WARM)
    
    # Contact & Response tracking
    contacted = models.BooleanField(default=False, db_index=True)
    first_contacted_at = models.DateTimeField(blank=True, null=True)
    last_call_at = models.DateTimeField(blank=True, null=True)
    current_response = models.CharField(max_length=50, blank=True, default="Not Contacted")

    # AI Lead Intelligence & AI Sales Assistant Fields
    ai_intent = models.CharField(max_length=150, blank=True, default="General Enquiry")
    ai_suggested_response = models.TextField(blank=True)
    ai_next_action = models.CharField(max_length=200, blank=True, default="Call to introduce course details")
    ai_summary = models.TextField(blank=True)

    scheduled_date = models.DateField(blank=True, null=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["phone"]),
            models.Index(fields=["status"]),
            models.Index(fields=["assigned_to"]),
            models.Index(fields=["priority"]),
            models.Index(fields=["contacted"]),
            models.Index(fields=["scheduled_date"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.phone})"

    @property
    def attempts_count(self):
        return self.call_logs.count()

    @property
    def is_contacted(self):
        return self.contacted or self.call_logs.exists()

    @property
    def assignee_display(self):
        if not self.assigned_to:
            return "—"
        return self.assigned_to.get_full_name() or self.assigned_to.username


class LeadAssignment(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        REASSIGNED = "REASSIGNED", "Reassigned"
        CLOSED = "CLOSED", "Closed"

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="assignments")
    caller = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="assignment_history",
        limit_choices_to={"role": User.Role.TELECALLER},
    )
    assigned_date = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    def __str__(self):
        return f"{self.lead.name} -> {self.caller}"


class DailySchedule(models.Model):
    caller = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="daily_schedules",
        limit_choices_to={"role": User.Role.TELECALLER}
    )
    target_date = models.DateField(db_index=True)
    daily_call_target = models.IntegerField(default=30)
    daily_conversion_target = models.IntegerField(default=5)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_daily_schedules")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-target_date", "caller"]
        unique_together = ("caller", "target_date")

    def __str__(self):
        return f"Schedule: {self.caller.username} on {self.target_date} (Target: {self.daily_call_target})"


class Followup(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DONE = "DONE", "Done"
        NO_RESPONSE = "NO_RESPONSE", "No Response"
        RESCHEDULED = "RESCHEDULED", "Rescheduled"

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="followups")
    caller = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="followups",
        limit_choices_to={"role": User.Role.TELECALLER},
    )
    scheduled_date = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="completed_followups")
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Followup: {self.lead.name} scheduled for {self.scheduled_date.strftime('%Y-%m-%d %H:%M')}"


class CallLog(models.Model):
    class Result(models.TextChoices):
        CONNECTED = "CONNECTED", "Connected"
        NOT_CONNECTED = "NOT_CONNECTED", "Not Connected"
        NOT_ANSWERED = "NOT_ANSWERED", "Not Answered 📵"
        BUSY = "BUSY", "Busy"
        SWITCHED_OFF = "SWITCHED_OFF", "Switched Off"
        WRONG_NUMBER = "WRONG_NUMBER", "Wrong Number"
        INVALID_NUMBER = "INVALID_NUMBER", "Invalid Number"
        NO_RESPONSE = "NO_RESPONSE", "No Response"
        INTERESTED = "INTERESTED", "Interested"
        NOT_INTERESTED = "NOT_INTERESTED", "Not Interested"
        CALLBACK_REQUESTED = "CALLBACK_REQUESTED", "Call Back Requested 📞"
        CONVERTED = "CONVERTED", "Converted 🎉"

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="call_logs")
    caller = models.ForeignKey(User, on_delete=models.CASCADE, related_name="call_logs")
    call_time = models.DateTimeField(default=timezone.now)
    duration_seconds = models.IntegerField(default=0)
    call_result = models.CharField(max_length=30, choices=Result.choices, default=Result.CONNECTED)
    remarks = models.TextField(blank=True)
    call_session_id = models.CharField(max_length=100, blank=True, null=True, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"CallLog: {self.lead.name} by {self.caller.username} ({self.call_result})"


class LeadActivity(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="activities")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    activity_type = models.CharField(max_length=50)
    description = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"Activity [{self.activity_type}]: {self.lead.name} at {self.timestamp}"


class WhatsAppTemplate(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    template_name = models.CharField(max_length=100, unique=True)
    meta_template_name = models.CharField(max_length=100)
    language = models.CharField(max_length=10, default="en")
    category = models.CharField(max_length=50, default="UTILITY")
    body_text = models.TextField()
    variables = models.JSONField(default=list, help_text="e.g. ['name', 'course', 'caller_name', 'company_name', 'fee']")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"WA Template: {self.template_name} ({self.status})"


class WhatsAppMessage(models.Model):
    class Direction(models.TextChoices):
        INBOUND = "INBOUND", "Inbound"
        OUTBOUND = "OUTBOUND", "Outbound"

    class Status(models.TextChoices):
        SENT = "SENT", "Sent"
        DELIVERED = "DELIVERED", "Delivered"
        READ = "READ", "Read"
        FAILED = "FAILED", "Failed"
        RECEIVED = "RECEIVED", "Received"

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="whatsapp_messages")
    message = models.TextField()
    direction = models.CharField(max_length=10, choices=Direction.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SENT)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["lead"]),
            models.Index(fields=["timestamp"]),
        ]

    def __str__(self):
        return f"{self.direction}: {self.message[:30]}"


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=150)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Notification for {self.user.username}: {self.title}"


class ExportHistory(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="exports")
    export_type = models.CharField(max_length=100, default="Lead Dataset Export")
    filters_applied = models.JSONField(default=dict, blank=True)
    record_count = models.IntegerField(default=0)
    exported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-exported_at"]

    def __str__(self):
        return f"Export by {self.user.username if self.user else 'System'} ({self.record_count} records) on {self.exported_at.strftime('%Y-%m-%d %H:%M')}"


class SecurityAuditLog(models.Model):
    class Action(models.TextChoices):
        USERNAME_CHANGED = "USERNAME_CHANGED", "Username Changed"
        PASSWORD_CHANGED = "PASSWORD_CHANGED", "Password Changed"
        OTHER_SESSIONS_LOGGED_OUT = "OTHER_SESSIONS_LOGGED_OUT", "Other Sessions Logged Out"
        LOGIN = "LOGIN", "User Login"
        LOGOUT = "LOGOUT", "User Logout"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="security_audit_logs")
    action = models.CharField(max_length=50, choices=Action.choices)
    ip_address = models.CharField(max_length=45, blank=True)
    user_agent = models.TextField(blank=True)
    details = models.TextField(blank=True)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = "Security Audit Log"
        verbose_name_plural = "Security Audit Logs"

    def __str__(self):
        return f"{self.user.username} - {self.get_action_display()} at {self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"


def log_security_event(user, action, request=None, details=""):
    ip_address = ""
    user_agent = ""
    if request:
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip_address = x_forwarded_for.split(",")[0].strip()
        else:
            ip_address = request.META.get("REMOTE_ADDR", "")
        user_agent = request.META.get("HTTP_USER_AGENT", "")
    return SecurityAuditLog.objects.create(
        user=user,
        action=action,
        ip_address=ip_address,
        user_agent=user_agent,
        details=details
    )


