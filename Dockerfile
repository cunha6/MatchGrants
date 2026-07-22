# Fixado em BOOKWORM (Debian 12). A tag "slim" mudou para trixie (Debian 13), cujo
# Chromium 150 rebenta em container com SIGTRAP ("Chrome instance exited"). O Chromium
# do bookworm é estável em headless/Docker. (bookworm = glibc 2.36, igual à imagem da BD.)
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y \
    poppler-utils \
    tesseract-ocr \
    libgl1 \
    gcc \
    chromium \
    chromium-driver \
    libnss3 \
    libfontconfig1 \
    libgbm1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Produção: gunicorn (o docker-compose de dev sobrepõe este CMD com runserver+migrate).
# Workers GTHREAD: o heartbeat corre no loop do processo, independente da duração dos
# pedidos — o scrape dos avisos pode demorar 10, 30, 60+ minutos sem ser morto.
# (Com workers `sync`, o --timeout mataria qualquer pedido mais longo que ele.)
# O --timeout fica assim só para o que deve: recuperar workers realmente crashados.
CMD ["gunicorn", "main.wsgi:application", "--bind", "0.0.0.0:8000", \
     "--workers", "2", "--worker-class", "gthread", "--threads", "4", "--timeout", "120"]