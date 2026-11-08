# Household Finance, self-hosted. This image only opens behind a reverse proxy that does the login and sends
# FINAPP_PROXY_SECRET in the X-Finapp-Proxy-Secret header; see "Self-hosting" in the README.
# On a desktop, use run.bat / run.sh instead.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FINAPP_MODE=server \
    FINAPP_HOME=/data

WORKDIR /app
COPY requirements.lock .
RUN pip install -r requirements.lock

RUN useradd --create-home --uid 10001 finapp \
    && mkdir -p /data \
    && chown finapp:finapp /data
COPY --chown=finapp:finapp . .

USER finapp
VOLUME ["/data"]
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"]

CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
