#!/bin/sh

set -e

# Carrega as configurações antes do migrate e initadmin
export DJANGO_SETTINGS_MODULE="${SETTINGS_FILE_PATH:-core.settings.dev}"
echo "Using settings: $DJANGO_SETTINGS_MODULE"

echo 'Waiting for database...'
python manage.py wait_for_database --timeout "${DB_WAIT_TIMEOUT_SECONDS:-60}" --interval "${DB_WAIT_INTERVAL_SECONDS:-2}"

echo 'Migrating database...'
python manage.py migrate --noinput

if [ "$DEBUG" = "True" ] || [ "$DEBUG" = "true" ] || [ "$DEBUG" = "1" ]; then
    echo "Starting Development Server..."
    exec python manage.py runserver 0.0.0.0:${PORT:-8000}
fi

echo "Starting Production Server..."
python manage.py collectstatic --noinput
exec gunicorn core.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 3