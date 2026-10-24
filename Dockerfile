FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

# Nicht als root laufen lassen. Der Code gehoert root und ist fuer die App nur
# lesbar. /app/data gehoert dem Benutzer schon im Image: Docker uebernimmt
# Besitzer und Rechte beim ersten Einbinden in das leere Volume.
RUN useradd --create-home --uid 10001 angebote \
    && mkdir -p /app/data /app/config \
    && chown angebote:angebote /app/data \
    && chmod 700 /app/data
USER angebote

EXPOSE 8098
# --forwarded-allow-ips: Hinter einem Reverse Proxy kommen Protokoll (https)
# und Client-IP aus X-Forwarded-Proto/-For. Davon haengen nur das Secure-Flag
# des Cookies und die IP im Protokoll ab, deshalb darf jeder Absender sie setzen.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8098", "--forwarded-allow-ips", "*"]
