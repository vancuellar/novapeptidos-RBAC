# Nova Peptidos RBAC

Backend for the Nova Peptidos storefront, including users, admin access, products, orders, and API routes.

## Run locally

```bash
pip install -r requirements.txt
uvicorn server:app --reload
```

## Hosting

The backend is ready for Docker hosting. It needs:

- `MONGO_URL`
- `DB_NAME`
- `JWT_SECRET`
- `CORS_ORIGINS`
- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`
- `OPENAI_API_KEY` only if the AI chat should answer with OpenAI

`render.yaml` is included for Render Blueprint deployment.

After the backend is live, set the UI build variable:

```bash
REACT_APP_BACKEND_URL=https://your-backend-url
```
