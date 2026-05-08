import asyncio
import json
import os
import re
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pydantic import BaseModel

# Load environment variables from .env
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ARXIV_ID_RE = re.compile(r"^(?P<id>\d{4}\.\d{4,5}(?:v\d+)?)$")
SYSTEM_PROMPT = """You are a research paper skeptic. You read academic papers like a senior reviewer who has seen too many overclaimed results — sharp, fair, and unwilling to be impressed by surface polish.

Your task has two parts.

PART 1 — A plain summary of what the paper actually claims.
4 to 6 sentences. Strip hype words. If the paper says "novel" or "state-of-the-art," translate that into the specific concrete claim. If the contribution is incremental, say so. If it's a survey, say so.

PART 2 — Up to three specific doubts.
Each doubt must be:
- Specific to this paper (not generic)
- Grounded in something visible in the text
- Categorized as one of: "missing_baseline", "suspicious_methodology", or "unstated_assumption"

For "missing_baseline": Name a specific competing method or dataset that should have been compared against and wasn't. Explain why its absence matters.
For "suspicious_methodology": Identify a specific decision (dataset selection, hyperparameter, evaluation metric, sample size, train/test split) that looks chosen to favor the result. Explain what alternative would be more honest.
For "unstated_assumption": Name something the paper takes for granted that, if false, would weaken the conclusion.

Rules:
- Do not be polite for politeness's sake.
- Do not invent claims that aren't in the paper.
- Do not raise generic doubts. Every doubt must be specific to THIS paper.
- Skip obvious objections the authors clearly addressed. Find what reviewers actually catch.
- If you genuinely cannot find three substantive doubts, return fewer. Padding is dishonest.

Respond with ONLY a JSON object in this exact schema, no other text:
{
  "summary": "string, 4-6 sentences",
  "doubts": [
    {
      "category": "missing_baseline" | "suspicious_methodology" | "unstated_assumption",
      "headline": "one sentence, direct, no hedging",
      "evidence": "2-3 sentences, specific to this paper, may reference sections"
    }
  ],
  "doubts_found": <number from 0 to 3>
}"""
CONFIGURED_MODEL = os.getenv("OPENAI", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MODEL_NAME = "gemini-3-flash-preview"

MIN_PDF_BYTES = 10 * 1024
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_GEMINI_WAIT_SECONDS = 60.0
PDF_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=20.0)
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "5"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "3600"))

_request_timestamps: dict[str, deque[float]] = defaultdict(deque)
_rate_limit_lock = asyncio.Lock()

DOUBT_CATEGORIES = {
    "missing_baseline",
    "suspicious_methodology",
    "unstated_assumption",
}

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "doubts", "doubts_found"],
    "properties": {
        "summary": {"type": "string"},
        "doubts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["category", "headline", "evidence"],
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": sorted(DOUBT_CATEGORIES),
                    },
                    "headline": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "doubts_found": {
            "type": "integer",
            "minimum": 0,
            "maximum": 3,
        },
    },
}

app = FastAPI(title="Paper Skeptic")

# Allow local frontend development origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class AnalyzeRequest(BaseModel):
    arxiv_url: str


def _client_identifier(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    if request.client and request.client.host:
        return request.client.host

    return "unknown"


async def _enforce_analyze_rate_limit(request: Request) -> None:
    client_id = _client_identifier(request)
    now = time.monotonic()

    async with _rate_limit_lock:
        timestamps = _request_timestamps[client_id]

        while timestamps and now - timestamps[0] > RATE_LIMIT_WINDOW_SECONDS:
            timestamps.popleft()

        if len(timestamps) >= RATE_LIMIT_REQUESTS:
            retry_after = max(1, int(RATE_LIMIT_WINDOW_SECONDS - (now - timestamps[0])))
            raise HTTPException(
                status_code=429,
                detail=(
                    "rate limit exceeded. "
                    f"max {RATE_LIMIT_REQUESTS} analyze requests per {RATE_LIMIT_WINDOW_SECONDS}s. "
                    f"try again in {retry_after}s."
                ),
                headers={"Retry-After": str(retry_after)},
            )

        timestamps.append(now)


def normalize_arxiv_pdf_url(arxiv_input: str) -> tuple[str, str]:
    raw_value = arxiv_input.strip()
    if not raw_value:
        raise HTTPException(status_code=400, detail="no url provided")

    # Accept raw IDs like 2301.00001 (optionally with version suffix).
    id_match = ARXIV_ID_RE.fullmatch(raw_value)
    if id_match:
        paper_id = id_match.group("id")
        return paper_id, f"https://arxiv.org/pdf/{paper_id}.pdf"

    parsed = urlparse(raw_value)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid arXiv input. Use an arXiv URL or raw ID like 2301.00001.",
        )

    if parsed.netloc.lower() not in {"arxiv.org", "www.arxiv.org"}:
        raise HTTPException(
            status_code=400,
            detail="URL is not an arXiv URL. Expected host arxiv.org.",
        )

    path = parsed.path.strip("/")
    paper_id = ""

    if path.startswith("abs/"):
        paper_id = path.removeprefix("abs/")
    elif path.startswith("pdf/"):
        paper_id = path.removeprefix("pdf/")
        if paper_id.endswith(".pdf"):
            paper_id = paper_id[:-4]

    if not ARXIV_ID_RE.fullmatch(paper_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid arXiv URL pattern. Supported forms: "
                "https://arxiv.org/abs/{id}, https://arxiv.org/pdf/{id}, "
                "https://arxiv.org/pdf/{id}.pdf, or raw {id}."
            ),
        )

    return paper_id, f"https://arxiv.org/pdf/{paper_id}.pdf"


def _call_gemini_for_pdf(
    pdf_bytes: bytes,
    strict_json: bool = False,
) -> Any:
    if not GEMINI_API_KEY:
        raise RuntimeError("Missing GEMINI_API_KEY in environment.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    user_text = "Analyze this paper and return only JSON that matches the requested schema."
    if strict_json:
        user_text = (
            f"{user_text} "
            "Respond ONLY with valid JSON. Do not include markdown, prose, or extra text."
        )

    temp_pdf_path = ""
    uploaded_file = None
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
        temp_file.write(pdf_bytes)
        temp_pdf_path = temp_file.name

    try:
        uploaded_file = client.files.upload(
            path=temp_pdf_path,
            config=types.UploadFileConfig(mime_type="application/pdf"),
        )
    finally:
        if temp_pdf_path and os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)

    if not uploaded_file or not getattr(uploaded_file, "uri", None):
        raise RuntimeError("Failed to upload PDF to Gemini Files API.")

    pdf_part = types.Part.from_uri(
        file_uri=uploaded_file.uri,
        mime_type=getattr(uploaded_file, "mime_type", "application/pdf"),
    )
    text_part = types.Part.from_text(text=user_text)

    try:
        return client.models.generate_content(
            model=MODEL_NAME,
            contents=[types.Content(role="user", parts=[pdf_part, text_part])],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
            ),
        )
    finally:
        # Best-effort cleanup of uploaded file resource.
        if uploaded_file and getattr(uploaded_file, "name", None):
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass


def _parse_and_validate_model_json(raw_text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model returned malformed JSON: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise HTTPException(status_code=502, detail="Model JSON must be an object.")

    summary = parsed.get("summary")
    doubts = parsed.get("doubts")
    doubts_found = parsed.get("doubts_found")

    if not isinstance(summary, str):
        raise HTTPException(status_code=502, detail="Model JSON missing string field: summary.")
    if not isinstance(doubts, list):
        raise HTTPException(status_code=502, detail="Model JSON missing array field: doubts.")
    if not isinstance(doubts_found, int) or not (0 <= doubts_found <= 3):
        raise HTTPException(
            status_code=502,
            detail="Model JSON field doubts_found must be an integer between 0 and 3.",
        )

    normalized_doubts: list[dict[str, str]] = []
    for item in doubts:
        if not isinstance(item, dict):
            raise HTTPException(status_code=502, detail="Each doubts item must be an object.")

        category = item.get("category")
        headline = item.get("headline")
        evidence = item.get("evidence")

        if category not in DOUBT_CATEGORIES:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Invalid doubts category from model. Allowed: "
                    "missing_baseline, suspicious_methodology, unstated_assumption."
                ),
            )
        if not isinstance(headline, str) or not isinstance(evidence, str):
            raise HTTPException(
                status_code=502,
                detail="Each doubts item must contain string fields: headline and evidence.",
            )

        normalized_doubts.append(
            {
                "category": category,
                "headline": headline,
                "evidence": evidence,
            }
        )

    return {
        "summary": summary,
        "doubts": normalized_doubts,
        "doubts_found": doubts_found,
    }


@app.get("/")
def read_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.post("/analyze")
async def analyze_paper(request: Request, payload: AnalyzeRequest) -> dict[str, Any]:
    await _enforce_analyze_rate_limit(request)

    if not payload.arxiv_url.strip():
        raise HTTPException(status_code=400, detail="no url provided")

    paper_id, pdf_url = normalize_arxiv_pdf_url(payload.arxiv_url)

    try:
        async with httpx.AsyncClient(timeout=PDF_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(pdf_url)
            response.raise_for_status()
            pdf_bytes = response.content
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to download arXiv PDF from {pdf_url}: {exc}",
        ) from exc

    pdf_size = len(pdf_bytes)
    if pdf_size < MIN_PDF_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Downloaded file is suspiciously small ({pdf_size} bytes). "
                f"Expected at least {MIN_PDF_BYTES} bytes."
            ),
        )
    if pdf_size > MAX_PDF_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Downloaded file is suspiciously large ({pdf_size} bytes). "
                f"Maximum allowed is {MAX_PDF_BYTES} bytes."
            ),
        )

    if not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=502,
            detail="Downloaded content is not a valid PDF document.",
        )

    async def _run_gemini(strict_json: bool) -> str:
        try:
            gemini_response = await asyncio.wait_for(
                asyncio.to_thread(_call_gemini_for_pdf, pdf_bytes, strict_json),
                timeout=MAX_GEMINI_WAIT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="Gemini request timed out. Please try again.",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Gemini API request failed: {exc}",
            ) from exc

        response_text_local = getattr(gemini_response, "text", None)
        if not response_text_local:
            raise HTTPException(
                status_code=502,
                detail="Gemini response did not contain JSON text output.",
            )

        return response_text_local

    first_response_text = await _run_gemini(strict_json=False)

    try:
        analysis = _parse_and_validate_model_json(first_response_text)
    except ValueError:
        second_response_text = await _run_gemini(strict_json=True)
        try:
            analysis = _parse_and_validate_model_json(second_response_text)
        except ValueError:
            return {
                "status": "ok",
                "message": "couldn't parse structured output",
                "input": {
                    "arxiv_url": payload.arxiv_url,
                    "paper_id": paper_id,
                    "normalized_pdf_url": pdf_url,
                    "pdf_size_bytes": pdf_size,
                    "model": MODEL_NAME,
                },
                "result": {
                    "raw_text": second_response_text,
                },
            }

    return {
        "status": "ok",
        "message": "Analysis complete.",
        "input": {
            "arxiv_url": payload.arxiv_url,
            "paper_id": paper_id,
            "normalized_pdf_url": pdf_url,
            "pdf_size_bytes": pdf_size,
            "model": MODEL_NAME,
        },
        "result": analysis,
    }
