import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Configuração por ambiente (produção define estas no .env; os defaults mantêm o dev igual).
# Em produção: DJANGO_SECRET_KEY=<aleatória>, DJANGO_DEBUG=false, DJANGO_ALLOWED_HOSTS=dominio.pt
DEBUG = os.getenv('DJANGO_DEBUG', 'true').lower() in ('true', '1', 'yes')

# SECRET_KEY assina as sessões E os tokens de reset de password (ver
# users.service.request_password_reset -> default_token_generator). A chave abaixo está no
# repositório: quem a conhece forja um token de reset para QUALQUER conta e toma-a. Por isso
# em produção (DEBUG=false) a variável é obrigatória — a app recusa arrancar sem ela em vez de
# degradar em silêncio para uma chave pública.
_DEV_SECRET_KEY = 'django-insecure-h6dqv)z6y#ekqe5$_ug+r^c8m#s09%xehaxcfo3ux6!h@p-cvw'
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY') or _DEV_SECRET_KEY

# Validação do header Host. Em dev fica o wildcard (o container é acedido por localhost, pelo
# IP da máquina ou pelo nome do serviço, conforme o sítio de onde se chama); em produção tem
# de ser explícito, senão o wildcard abriria a porta a Host-header/cache poisoning.
ALLOWED_HOSTS = [
    h.strip() for h in os.getenv('DJANGO_ALLOWED_HOSTS', '*' if DEBUG else '').split(',')
    if h.strip()
]

if not DEBUG:
    if SECRET_KEY == _DEV_SECRET_KEY:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY tem de estar definida quando DEBUG=false — a chave de "
            "desenvolvimento está no repositório e permitiria forjar tokens de reset de "
            "password de qualquer conta."
        )
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured(
            "DJANGO_ALLOWED_HOSTS tem de estar definida quando DEBUG=false "
            "(ex: DJANGO_ALLOWED_HOSTS=matchgrants.pt,www.matchgrants.pt)."
        )

# Endurecimento automático quando DEBUG=false (produção atrás de HTTPS).
if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = os.getenv('DJANGO_COOKIES_SECURE', 'true').lower() == 'true'
    CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE

# Base do site do front-end (SPA à parte, não servido pelo Django) — usada para montar links
# absolutos em emails (ex: reset de password). Sem barra final.
FRONTEND_URL = os.getenv('FRONTEND_URL', 'http://localhost:5173').rstrip('/')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'avisos',
    'users',
    'match',
    'anuncios',
    'planned_grants',
    'newsletter',
]

# Chave(s) da API nif.pt — lidas do .env. A principal (NIF_KEY) mais alternativas opcionais
# (NIF_KEY1..NIF_KEY4). O serviço de match roda entre elas, uma de cada vez, para distribuir
# os pedidos por várias chaves (limites de utilização da API nif.pt).
NIF_KEY = os.getenv('NIF_KEY')
NIF_KEYS = [
    k for k in (
        os.getenv('NIF_KEY'),
        os.getenv('NIF_KEY1'),
        os.getenv('NIF_KEY2'),
        os.getenv('NIF_KEY3'),
        os.getenv('NIF_KEY4'),
    ) if k
]
BASE_KEY = os.getenv('BASE_KEY')
CTT_KEY = os.getenv('CTT_KEY')
# Chaves do OpenRouter (validação LLM dos matches): são estas DUAS e mais nenhuma. A validação
# roda entre elas a cada chamada, para não esgotar o limite gratuito de uma só. Uma chave em
# falta no .env é simplesmente descartada — com uma só configurada, não há rotação.
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_API_KEYS = [
    k for k in (
        os.getenv('OPENROUTER_API_KEY'),
        os.getenv('OPENROUTER_API_KEY1'),
    ) if k
]

# --- Email (Brevo — SMTP relay, evita os bloqueios de basic auth do Microsoft 365) ---
# Em dev (DEBUG) o backend por defeito é a consola: os emails são impressos no log em vez
# de enviados. Em produção usa SMTP com as credenciais do .env (EMAIL_HOST + Brevo).
EMAIL_BACKEND = os.getenv(
    'DJANGO_EMAIL_BACKEND',
    'django.core.mail.backends.console.EmailBackend' if DEBUG
    else 'django.core.mail.backends.smtp.EmailBackend',
)
EMAIL_HOST = os.getenv('EMAIL_HOST', '')
# `or 587` (em vez de um default no getenv): uma linha `EMAIL_PORT=` vazia no .env devolve ""
# — e não o default —, e int("") rebentava o arranque da app.
EMAIL_PORT = int(os.getenv('EMAIL_PORT') or 587)
# BREVO_SMTP_LOGIN/KEY são as credenciais da SMTP key do Brevo (Settings -> SMTP & API no
# painel Brevo) — não são a password da conta. EMAIL_HOST_USER/PASSWORD ficam como fallback
# genérico, para o caso de se voltar a trocar de fornecedor SMTP no futuro.
EMAIL_HOST_USER = os.getenv('BREVO_SMTP_LOGIN') or os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('BREVO_SMTP_KEY') or os.getenv('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'true').lower() == 'true'
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', '')

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'main.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'main.wsgi.application'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('DB_NAME'),
        'USER': os.getenv('DB_USER'),
        'PASSWORD': os.getenv('DB_PASSWORD'),
        'HOST': os.getenv('DB_HOST'),
        'PORT': os.getenv('DB_PORT'),
    },
    # Base de dados só-leitura de enriquecimento por NIF (empregados, proveitos, região)
    # carregada do dictionary_by_nif.json. SQLite indexado por NIF — 454k registos.
    'nif': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'nif_data.sqlite3',
    },
}

# Encaminha o modelo match.NifCompany para a BD 'nif'; tudo o resto vai para 'default'.
DATABASE_ROUTERS = ['match.routers.NifRouter']

# Cache PARTILHADO entre processos (tabela no Postgres). O default do Django é o LocMemCache,
# que vive dentro de cada worker: com os 2 workers do gunicorn (ver Dockerfile), o throttle do
# pedido de reset de password (users.views) permitiria o dobro dos emails, e o match retido à
# espera do contacto (match.services) só seria encontrado se o 2º pedido calhasse no mesmo
# worker. Ambos precisam de um estado único — daí a BD, que já existe (evita mais um serviço).
# A tabela é criada por `python manage.py createcachetable` (já no arranque do docker-compose).
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'django_cache',
    },
}

# Logging para a consola (docker logs): erros das apps ficam registados com stack trace
# em vez de se perderem — os handlers de exceção das views devolvem mensagens limpas.
# Os logs de 'avisos' (o que a IA gerou + auditoria de quem editou o quê) e de 'anuncios'
# (import + auditoria de edições) vão TAMBÉM para logs/*.log — a pasta está no volume do
# compose, por isso persiste no host.
LOGS_DIR = BASE_DIR / 'logs'
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {'format': '[{levelname}] {asctime} {name}: {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'simple'},
        'avisos_file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(LOGS_DIR / 'avisos.log'),
            'maxBytes': 5 * 1024 * 1024,   # 5 MB por ficheiro
            'backupCount': 5,
            'encoding': 'utf-8',
            'formatter': 'simple',
        },
        'anuncios_file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(LOGS_DIR / 'anuncios.log'),
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 5,
            'encoding': 'utf-8',
            'formatter': 'simple',
        },
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        # Apanha avisos.db_service (extração IA) e avisos.audit (edições) — consola + ficheiro.
        'avisos': {'handlers': ['console', 'avisos_file'], 'level': 'INFO', 'propagate': False},
        # Apanha anuncios.services (import/cadernos) e anuncios.audit (edições).
        'anuncios': {'handlers': ['console', 'anuncios_file'], 'level': 'INFO', 'propagate': False},
    },
}

LANGUAGE_CODE = 'pt-pt'
TIME_ZONE = 'Europe/Lisbon'
USE_I18N = True
USE_TZ = True
STATIC_URL = 'static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

