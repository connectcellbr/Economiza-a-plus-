FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=10000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 10000
CMD ["python", "servidor_precos.py"]
