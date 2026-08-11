# ltengine-py

Python port of [LTEngine](https://github.com/afanasjev82/LTEngine) `--features api` mode.

A LibreTranslate-compatible REST translation server that proxies requests to an external OpenAI-compatible LLM (e.g., vLLM, llama-server, Ollama, OpenAI). No local model loading — just a running LLM endpoint.

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)
- An external OpenAI-compatible LLM server

## Quick Start

```bash
# Install dependencies
uv sync

# Start server (minimum: --llm-url is required)
uv run ltengine --llm-url http://localhost:8000

# With explicit model and port
uv run ltengine --llm-url http://localhost:8000 --llm-model my-model --port 5050
```

## API

Fully compatible with the [LibreTranslate API](https://libretranslate.com/docs/):

| Endpoint | Method | Description |
|---|---|---|
| `/translate` | POST | Translate text |
| `/detect` | POST | Detect language |
| `/languages` | GET | List supported languages |
| `/health` | GET | Health check |
| `/frontend/settings` | GET | Frontend config |

### Translate

```bash
curl -X POST http://localhost:5050/translate \
  -H "Content-Type: application/json" \
  -d '{"q": "Hello!", "source": "en", "target": "es"}'
# {"translatedText": "¡Hola!"}
```

### Auto-detect Source Language

```bash
curl -X POST http://localhost:5050/translate \
  -H "Content-Type: application/json" \
  -d '{"q": "Ciao!", "source": "auto", "target": "en"}'
# {"translatedText": "Bye!", "detectedLanguage": {"language": "it", "confidence": 83}}
```

## CLI Options

| Flag | Default | Description |
|---|---|---|
| `--llm-url` | *(required)* | OpenAI-compatible API base URL |
| `--llm-model` | *(auto)* | Model name (auto-resolved from `/v1/models` if empty) |
| `--llm-api-key` | `` | API key for the LLM server |
| `--host` | `0.0.0.0` | Bind host |
| `--port`, `-p` | `5050` | Bind port |
| `--char-limit` | `5000` | Max characters per request |
| `--api-key` | `` | Optional API key to protect this server |
| `--verbose`, `-v` | off | Verbose logging |

## Project Structure

```
ltengine-py/
├── backend/          # Python package: ltengine
│   └── src/ltengine/
│       ├── app.py          # Litestar application
│       ├── languages.py    # Language registry + detection
│       ├── prompt.py       # Translation prompt builder
│       ├── llm_client.py   # OpenAI-compatible HTTP client
│       └── __main__.py     # Entry point
└── frontend/         # Static web UI
    └── src/ltengine_frontend/static/
        ├── index.html
        ├── app.js
        └── style.css
```
