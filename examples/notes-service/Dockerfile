FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_PORT=8081

WORKDIR /app
COPY app/main.py ./main.py

EXPOSE 8081

HEALTHCHECK --interval=5s --timeout=2s --start-period=2s --retries=5 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"APP_PORT\", \"8081\")}/ready', timeout=1)"

CMD ["python", "main.py"]
