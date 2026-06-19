# AI Chat Widget MVP

Embeddable AI chat widget MVP for a medical/cosmetology center.

## Current Status
- Project scaffold created
- Demo knowledge base added
- Backend and widget implementation pending

## Run
```bash
cp .env.example .env
docker compose up --build
```

## LLM Provider Options
- Default: `LLM_PROVIDER=mock` for deterministic local demo behavior
- OpenAI-compatible: set `LLM_PROVIDER=openai` and `LLM_API_KEY`
- Gemini test setup:

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_key
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_MODEL=gemini-3.5-flash
```

- You can also use provider-agnostic vars directly:

```env
LLM_PROVIDER=openai_compatible
LLM_API_KEY=your_key
LLM_BASE_URL=https://your-endpoint.example/v1
LLM_MODEL=your-model-name
```

## Development
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```
