FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY harbor/ harbor/
COPY site/ site/

# Cloud Run injects PORT; default 8080 locally
ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
