FROM python:3.13-slim

# Forzar DNS de Google en build-time
RUN echo "nameserver 8.8.8.8" > /etc/resolv.conf

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Forzar DNS de Google en runtime (Railway puede sobrescribir resolv.conf al arrancar)
CMD echo "nameserver 8.8.8.8" > /etc/resolv.conf && \
    exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
