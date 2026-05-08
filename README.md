# Paper Skeptic

Paper Skeptic is a small FastAPI app that takes an arXiv link, downloads the paper PDF, sends it to Gemini, and returns a structured skeptical critique.

The interface is editorial-style and intentionally minimal.

## What it does

- Accepts arXiv inputs in these forms:
	- https://arxiv.org/abs/2301.00001
	- https://arxiv.org/pdf/2301.00001
	- https://arxiv.org/pdf/2301.00001.pdf
	- 2301.00001
- Normalizes to the PDF URL and downloads in memory (no local PDF storage in request flow).
- Validates downloaded file size and PDF signature.
- Sends the paper to Gemini and asks for strict JSON output.
- Retries once with stricter JSON instruction if parsing fails.
- Returns structured response for frontend rendering.

## Tech stack

- Backend: FastAPI
- Frontend: plain HTML, CSS, and vanilla JS
- HTTP client: httpx
- LLM SDK: google-genai
- Env loading: python-dotenv

## Project structure

- main.py: API app and analysis pipeline
- static/index.html: UI markup
- static/style.css: custom styles
- static/app.js: frontend behavior
- .env: secrets and runtime config

## Requirements

- Python 3.10+
- A valid Gemini API key

Install dependencies:

python -m pip install -r requirements.txt

## Environment variables

Set in .env:

- GEMINI_API_KEY=your_key_here
- OPENAI=optional_model_override
- RATE_LIMIT_REQUESTS=5
- RATE_LIMIT_WINDOW_SECONDS=3600

Notes:

- Current code uses gemini-3-flash-preview as the model constant.
- OPENAI is still read for compatibility with earlier wiring, but can be left empty.

## Run locally

python -m uvicorn main:app --host 127.0.0.1 --port 8000

Open:

- http://127.0.0.1:8000

## API

### POST /analyze

Request body:

{
	"arxiv_url": "https://arxiv.org/abs/2301.00001"
}

Success response includes:

- status
- message
- input metadata
- result object with:
	- summary
	- doubts[]
	- doubts_found

## Errors and safeguards

- 400 for invalid input patterns and invalid file size
- 429 for rate limit exceeded
- 502 for download/model failures or invalid model output
- 504 when Gemini call times out

The app includes an in-memory IP-based rate limiter for /analyze to protect API credits.

## Deployment note

Current rate limiting is in-memory, which is fine for a single instance.
For multi-instance deployment, use a shared store (for example Redis) for global limits.
