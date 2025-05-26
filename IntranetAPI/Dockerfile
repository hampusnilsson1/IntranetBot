FROM python:3.11-alpine

# Ange arbetskatalogen som /app
WORKDIR /app

# Kopiera beroenden och installera dem
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiera hela projektet till containern
COPY . .

# Byt arbetskatalog till src, där Python-skriptet finns
WORKDIR /app/src

# Exponera port 3003
EXPOSE 3034

# Starta Python-skriptet
CMD ["uvicorn", "chat_api:asgi_app", "--host", "0.0.0.0", "--port", "3034"]

