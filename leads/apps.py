from django.apps import AppConfig
from django.db.models.signals import post_migrate


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

        # Ensure default TELECALLER user pawan exists
        pawan_user = User.objects.filter(username="pawan").first()
        if not pawan_user:
            User.objects.create_user(
                username="pawan",
                password="callerpassword",
                role=User.Role.TELECALLER,
                status="ACTIVE"
            )
    except Exception:
        pass


class LeadsConfig(AppConfig):
    name = 'leads'

    def ready(self):
        post_migrate.connect(auto_seed_default_users, sender=self)

