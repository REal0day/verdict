# ---------- stage 1: build the React frontend ----------
FROM node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund --loglevel=error
COPY frontend/ ./
RUN npm run build

# ---------- stage 2: python runtime ----------
FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/app ./app
# Vendor the agent source so the server can hand it out via /ui/agent/source.tar.gz
COPY agent ./agent_src
# Built SPA goes under app/static so FastAPI can mount it.
COPY --from=frontend /fe/dist ./app/static/spa
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
