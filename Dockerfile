FROM python:3.9-slim-bullseye

WORKDIR /app

# No se instala ningun paquete del sistema.
# SSH se realiza con Paramiko desde Python y los comandos MariaDB/Galera
# se ejecutan remotamente en cada nodo.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_PROGRESS_BAR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --no-cache-dir --progress-bar off -r requirements.txt

COPY app ./app
COPY docs ./docs

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8080"]
