import os
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.conf import settings

class Command(BaseCommand):
    help = "Export all local CRM data to JSON fixture for production migration"

    def handle(self, *args, **options):
        fixture_dir = os.path.join(settings.BASE_DIR, "leads", "fixtures")
        os.makedirs(fixture_dir, exist_ok=True)
        fixture_path = os.path.join(fixture_dir, "local_crm_export.json")

        self.stdout.write("Exporting local CRM data...")
        with open(fixture_path, "w", encoding="utf-8") as f:
            call_command("dumpdata", "leads", indent=2, stdout=f)
        
        self.stdout.write(self.style.SUCCESS(f"Successfully exported CRM data to {fixture_path}"))
