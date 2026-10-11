FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    TAJ_DB_PATH=/data/taj-opticals.sqlite3 \
    TAJ_UPLOAD_DIR=/data/uploads
WORKDIR /app
COPY server.py /app/server.py
COPY static /app/static
COPY railway_entrypoint.py /app/railway_entrypoint.py
RUN useradd --system --uid 10001 --create-home app \
    && mkdir -p /data/uploads \
    && chown -R app:app /app /data
USER 10001:10001
EXPOSE 8000
VOLUME ["/data"]
CMD ["python3", "/app/railway_entrypoint.py"]
