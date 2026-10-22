FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /controller

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY controller/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --timeout=120 --retries=5 -r requirements.txt

COPY controller/main.py ./main.py
COPY controller/risk_model.py ./risk_model.py
COPY controller/dashboard.html ./dashboard.html
COPY controller/zerodown ./zerodown
COPY config ./config

EXPOSE 9000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
