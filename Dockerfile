FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY static ./static

# No --bind: gunicorn binds to 0.0.0.0:$PORT by itself when PORT is set.
CMD ["gunicorn", "app:app", "--workers", "1", "--threads", "8", "--timeout", "60"]
