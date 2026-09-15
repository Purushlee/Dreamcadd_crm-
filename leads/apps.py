import sys
from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.core.management import call_command


def auto_seed_default_users(sender, **kwargs):
    try:
        from leads.models import User
        # Ensure default MD user DREAMCADD exists
        md_user = User.objects.filter(username="DREAMCADD").first()
        if not md_user:
            User.objects.create_superuser(
                username="DREAMCADD",
                password="admin123",
                role=User.Role.MD,
                status="ACTIVE"
            )
        
        # Ensure default ADMIN user admin exists
        admin_user = User.objects.filter(username="admin").first()
        if not admin_user:
            User.objects.create_superuser(
                username="admin",
                password="admin123",
                role=User.Role.ADMIN,
                status="ACTIVE"
            )

        # Import all local CRM fixture data in non-test environments
        if "test" not in sys.argv:
            call_command("import_crm_data")
    except Exception as e:
        print(f"Auto-seed error: {e}")


class LeadsConfig(AppConfig):
    name = 'leads'

    def ready(self):
        post_migrate.connect(auto_seed_default_users, sender=self)

