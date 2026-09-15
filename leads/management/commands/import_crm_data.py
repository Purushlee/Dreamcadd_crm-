import os
from django.core.management.base import BaseCommand
from django.core.serializers import deserialize
from django.conf import settings
from leads.models import User

class Command(BaseCommand):
    help = "Safely import/update local CRM data from fixture into database without deleting existing data"

    def handle(self, *args, **options):
        fixture_path = os.path.join(settings.BASE_DIR, "leads", "fixtures", "local_crm_export.json")
        if not os.path.exists(fixture_path):
            self.stdout.write(self.style.WARNING(f"Fixture file not found at {fixture_path}"))
            return

        self.stdout.write("Importing CRM data from fixture...")
        count = 0
        user_count = 0

        with open(fixture_path, "r", encoding="utf-8") as f:
            data = f.read()

        objects = list(deserialize("json", data, ignorenonexistent=True))

        # Import Users first to ensure ForeignKeys resolve
        user_objects = [obj for obj in objects if obj.object._meta.model_name == "user"]
        other_objects = [obj for obj in objects if obj.object._meta.model_name != "user"]

        for obj in user_objects:
            u_inst = obj.object
            # Update or create user by username
            existing = User.objects.filter(username=u_inst.username).first()
            if existing:
                existing.password = u_inst.password
                existing.role = u_inst.role
                existing.status = u_inst.status
                existing.is_active = u_inst.is_active
                existing.is_superuser = u_inst.is_superuser
                existing.is_staff = u_inst.is_staff
                existing.email = u_inst.email
                existing.phone = u_inst.phone
                existing.first_name = u_inst.first_name
                existing.last_name = u_inst.last_name
                existing.save()
            else:
                u_inst.save()
            user_count += 1
            count += 1

        for obj in other_objects:
            try:
                obj.save()
                count += 1
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Skipping {obj.object}: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} records ({user_count} users) from fixture."))
