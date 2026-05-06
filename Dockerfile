FROM python:3.11-slim

WORKDIR /app

# System deps for yfinance / requests
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Kolkata
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# Bind Flask to all interfaces inside the container
ENV FLASK_HOST=0.0.0.0
CMD ["python", "web_app.py"]
