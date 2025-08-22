FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Версию можно переопределять: --build-arg TINYMCE_VERSION=6.8.6
ARG TINYMCE_VERSION=6.8.6

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl unzip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Директория с данными (монтируется volume-ом)
RUN mkdir -p /data
ENV USERS_FILE=/data/user.txt
ENV SECRET_KEY=change_this_secret

# Код приложения
COPY app /app/app

# Локальная установка TinyMCE из tinymce-dist (корень архива, НЕ /tinymce/)
RUN set -eux; \
    mkdir -p /app/app/static/vendor/tinymce; \
    curl -fsSL -o /tmp/tinymce.zip \
      "https://codeload.github.com/tinymce/tinymce-dist/zip/refs/tags/${TINYMCE_VERSION}"; \
    unzip -q /tmp/tinymce.zip -d /tmp; \
    src="/tmp/tinymce-dist-${TINYMCE_VERSION}"; \
    cp -r "$src"/* /app/app/static/vendor/tinymce/; \
    test -f /app/app/static/vendor/tinymce/tinymce.min.js; \
    rm -rf "$src" /tmp/tinymce.zip

RUN set -eux; \
  mkdir -p /app/app/static/vendor/tinymce/langs; \
  curl -fsSL \
    "https://cdn.jsdelivr.net/npm/tinymce-i18n@25.8.4/langs6/ru.js" \
    -o /app/app/static/vendor/tinymce/langs/ru.js

EXPOSE 8000
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8000", "app.app:app"]
