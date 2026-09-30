from django.core.management.base import BaseCommand
from django.db import transaction
from leads.models import User, Lead, LeadAssignment, CallLog, Followup, DailySchedule, UserSessionLog, Notification


class Command(BaseCommand):
    help = "Permanently removes all Telecaller user accounts and their related data from the database."

    def handle(self, *args, **options):
        telecallers = User.objects.filter(role=User.Role.TELECALLER)
        count = telecallers.count()

        if count == 0:
            self.stdout.write(self.style.WARNING("No telecaller accounts found. Nothing to delete."))
            return

        self.stdout.write(f"Found {count} telecaller(s):")
        for tc in telecallers:
            self.stdout.write(f"  - {tc.username} ({tc.get_full_name()})")

        try:
            with transaction.atomic():
                # Unassign all leads that are assigned to telecallers first
                # so the leads remain in the pool
                assigned_leads = Lead.objects.filter(assigned_to__in=telecallers)
                unassigned_count = assigned_leads.count()
                assigned_leads.update(
                    assigned_to=None,
                    allocation_batch=None,
                    status=Lead.Status.NEW,
                )
                self.stdout.write(f"  Unassigned {unassigned_count} leads back to pool.")

                # Delete related telecaller data
                DailySchedule.objects.filter(caller__in=telecallers).delete()
                Notification.objects.filter(user__in=telecallers).delete()
                UserSessionLog.objects.filter(user__in=telecallers).delete()

                # Delete the telecaller accounts
                telecallers.delete()

            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully deleted {count} telecaller account(s). "
                    f"{unassigned_count} leads returned to the unallocated pool."
                )
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error: {e}"))
            raise
