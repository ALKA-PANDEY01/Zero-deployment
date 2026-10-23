FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /controller

COPY controller/requirements.txt ./requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

COPY controller/main.py ./main.py
COPY controller/dashboard.html ./dashboard.html
COPY controller/risk_model.py ./risk_model.py
COPY controller/zerodown ./zerodown
COPY config/zerodown.yaml /config/zerodown.yaml

EXPOSE 9000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000", "--timeout-graceful-shutdown", "40"]
