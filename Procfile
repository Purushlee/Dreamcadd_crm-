release: python manage.py migrate --noinput
web: python manage.py migrate --noinput && gunicorn dreamcadd.wsgi:application

