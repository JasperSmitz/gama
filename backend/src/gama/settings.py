"""Django settings for Gama.

Development: ``GAMA_DEBUG=1`` enables DEBUG and a fixed insecure secret key.
Production (``GAMA_DEBUG`` unset): ``GAMA_SECRET_KEY`` is required; set
``GAMA_SECURE_COOKIES=1`` when served behind TLS.
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from gama.config import load_config

BASE_DIR = Path(__file__).resolve().parent

GAMA = load_config()

DEBUG = os.environ.get("GAMA_DEBUG", "") == "1"
SECRET_KEY = os.environ.get("GAMA_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("GAMA_SECRET_KEY must be set when GAMA_DEBUG is not 1")
    SECRET_KEY = "django-insecure-gama-development-only"

ALLOWED_HOSTS = ["127.0.0.1", "localhost"] + [
    h for h in os.environ.get("GAMA_ALLOWED_HOSTS", "").split(",") if h
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Every view requires login unless explicitly marked @login_not_required.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "gama.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "gama.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": GAMA.database_path,
        "OPTIONS": {
            # WAL lets the web process read while the worker writes;
            # synchronous=FULL makes every commit durable (safety over speed).
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

if os.environ.get("GAMA_SECURE_COOKIES", "") == "1":
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
