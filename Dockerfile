FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY static ./static

# Shell form so $PORT, which Railway sets at runtime, is expanded.
CMD exec gunicorn app:app --workers 1 --threads 8 --timeout 60 --bind 0.0.0.0:$PORT
