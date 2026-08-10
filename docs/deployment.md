# Deployment Guide: Revenue Guardian

This project can run as two services from the same image:

1. `rg-backend`: FastAPI backend from `main:app` on port `8000`.
2. `rg-dashboard`: Streamlit dashboard from `app.py` on port `8501`.

## Local Docker

```bash
cp .env.example .env
docker compose up --build
```

Open:

- Dashboard: `http://localhost:8501`
- Backend health: `http://localhost:8000/health`

## Google Cloud Run Outline

Enable the APIs you need:

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com
```

Build and push:

```bash
gcloud artifacts repositories create revenue-guardian --repository-format=docker --location=us-central1
gcloud builds submit --tag us-central1-docker.pkg.dev/YOUR_PROJECT_ID/revenue-guardian/app:latest .
```

Deploy backend:

```bash
gcloud run deploy rg-backend \
  --image=us-central1-docker.pkg.dev/YOUR_PROJECT_ID/revenue-guardian/app:latest \
  --command=uvicorn \
  --args=main:app,--host,0.0.0.0,--port,8000 \
  --region=us-central1 \
  --port=8000 \
  --allow-unauthenticated \
  --set-env-vars=DATABASE_URL=sqlite:///crm.db,USE_LIVE_ADK=false
```

Deploy dashboard:

```bash
gcloud run deploy rg-dashboard \
  --image=us-central1-docker.pkg.dev/YOUR_PROJECT_ID/revenue-guardian/app:latest \
  --command=streamlit \
  --args=run,app.py,--server.port=8501,--server.address=0.0.0.0 \
  --region=us-central1 \
  --port=8501 \
  --allow-unauthenticated \
  --set-env-vars=BACKEND_URL=https://YOUR_BACKEND_URL
```

For live ADK/Gemini, store `GEMINI_API_KEY` and `JWT_SECRET_KEY` in Secret Manager and set `USE_LIVE_ADK=true` only after validating the ADK dependency version.
