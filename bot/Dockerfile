FROM node:20-alpine AS webapp-build
WORKDIR /webapp
COPY webapp/package.json webapp/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY webapp/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY .env.example /app/.env.example
COPY --from=webapp-build /webapp/dist /app/webapp_dist

ENV WEBAPP_DIST_DIR=/app/webapp_dist

CMD ["python", "-m", "app"]
