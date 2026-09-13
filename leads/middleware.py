from django.utils import timezone
from .models import UserSessionLog


class TelecallerActivityMiddleware:
    """
    Middleware that updates request.user.last_activity on every authenticated request,
    and updates or creates the active UserSessionLog entry for today's session.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            now = timezone.now()
            # Update user's last_activity timestamp
            request.user.last_activity = now
            request.user.save(update_fields=["last_activity"])

            # Maintain session log
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            active_session = UserSessionLog.objects.filter(
                user=request.user,
                login_time__gte=today_start,
                logout_time__isnull=True
            ).first()

            if not active_session:
                ip_addr = request.META.get("REMOTE_ADDR", "")
                UserSessionLog.objects.create(
                    user=request.user,
                    login_time=now,
                    last_activity=now,
                    ip_address=ip_addr
                )
            else:
                active_session.last_activity = now
                active_session.save(update_fields=["last_activity"])

        response = self.get_response(request)
        return response
