web: python manage.py collectstatic --noinput && gunicorn autotestAPI.wsgi --bind 0.0.0.0:$PORT --workers 3 --timeout 120
release: python manage.py migrate --noinput
