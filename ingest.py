#!/usr/bin/env python3
"""
Document ingestion — text extraction and LLM proposition extraction.

Two modes:
  ephemeral  — raw text injected into the current conversation's system prompt only
  persistent — LLM extracts atomic propositions stored permanently in memory
"""

import io
import json
from pathlib import Path

import httpx


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

INGEST_SYSTEM_PROMPT = """\
You are a knowledge extraction system. Analyse the document provided and extract ALL meaningful information as atomic propositions, organised into logical sections.

Return ONLY a valid JSON object with this exact structure:
{
  "doc_summary": "One sentence describing the entire document",
  "sections": [
    {
      "name": "Section Name",
      "summary": "One sentence describing what this section covers",
      "propositions": [
        {
          "summary": "A single atomic, self-contained factual statement (max 180 chars)",
          "category": "fact|procedure|definition|reference|warning|specification",
          "priority": 3
        }
      ]
    }
  ]
}

Rules:
- Every proposition must be self-contained — a reader with no prior context can fully understand it on its own
- Include the subject explicitly in each proposition
- Maximum 180 characters per proposition summary
- Priority scale: 5=critical, 4=important, 3=useful, 2=minor (omit 1-2)
- Minimum priority to include: 3
- Group propositions into sections that match the document's natural structure; if the document has no sections, create logical groupings yourself
- Order propositions within each section in logical reading order (prerequisites before dependents)
- Do not invent information not present in the document"""


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_text(filename: str, data: bytes) -> str:
    """
    Extract plain text from file bytes. Returns empty string on failure.
    Tries pymupdf then pypdf for PDFs; python-docx for DOCX; UTF-8 for everything else.
    """
    ext = Path(filename).suffix.lower()

    if ext in (".txt", ".md", ".csv", ".log", ".rst", ".text"):
        return data.decode("utf-8", errors="replace")

    if ext == ".pdf":
        # Prefer pymupdf (faster, better layout), fall back to pypdf
        try:
            import pymupdf  # type: ignore
            doc = pymupdf.open(stream=data, filetype="pdf")
            pages = [page.get_text() for page in doc]
            return "\n\n".join(p for p in pages if p.strip())
        except Exception:
            pass
        try:
            import pypdf  # type: ignore
            reader = pypdf.PdfReader(io.BytesIO(data))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n\n".join(p for p in pages if p.strip())
        except Exception:
            return ""

    if ext == ".docx":
        try:
            import docx  # type: ignore
            doc = docx.Document(io.BytesIO(data))
            return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception:
            return ""

    # Generic fallback — try UTF-8
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# LLM proposition extraction
# ---------------------------------------------------------------------------

async def ingest_document(
    text: str,
    doc_name: str,
    client: httpx.AsyncClient,
    llm_url: str,
    model: str,
) -> dict:
    """
    Run LLM proposition extraction on document text.

    With a 32K-token context window (~128K chars), most documents fit in one
    pass. We reserve ~8K tokens for the output and system prompt, leaving
    ~24K tokens (~96K chars) for the document body.

    Returns the parsed ingestion result dict:
      { doc_name, doc_summary, sections: [ { name, summary, propositions: [...] } ] }

    Raises httpx.HTTPStatusError or json.JSONDecodeError on failure.
    """
    max_chars = 96_000
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    user_content = f"Document name: {doc_name}\n\n---\n\n{text}"
    if truncated:
        user_content += f"\n\n[Document truncated — only the first {max_chars} characters were processed]"

    print(f"[Ingest] LLM call: {llm_url} model={model} chars={len(user_content)}", flush=True)

    try:
        response = await client.post(
            f"{llm_url}/v1/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": INGEST_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": 8000,
                "stream": False,
            },
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=5.0),
        )
        print(f"[Ingest] LLM response: status={response.status_code} size={len(response.content)}", flush=True)
        if response.status_code != 200:
            print(f"[Ingest] Error body: {response.text[:500]}", flush=True)
        response.raise_for_status()
    except BaseException as exc:
        print(f"[Ingest] HTTP request failed: {type(exc).__name__}: {exc}", flush=True)
        raise

    try:
        resp_json = response.json()
    except Exception as exc:
        print(f"[Ingest] Failed to parse response as JSON: {type(exc).__name__}: {exc}", flush=True)
        print(f"[Ingest] Raw body: {response.text[:500]}", flush=True)
        raise

    msg = resp_json["choices"][0]["message"]
    raw = (msg.get("content") or msg.get("reasoning_content") or "").strip()
    if not raw:
        raise ValueError("LLM returned empty content and empty reasoning_content")
    print(f"[Ingest] Raw content ({len(raw)} chars, first 300): {raw[:300]!r}", flush=True)

    # Strip Qwen3 / DeepSeek thinking tokens <think>...</think>
    if "<think>" in raw:
        import re as _re
        raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
        print(f"[Ingest] After think-strip ({len(raw)} chars, first 200): {raw[:200]!r}", flush=True)

    # Strip markdown code fences if the model wraps the JSON
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    # Extract JSON object — handles preamble like "Thinking Process:" or prose before the JSON
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]
    elif start == -1:
        raise ValueError(
            f"No JSON object found in LLM response. "
            f"First 300 chars: {raw[:300]!r}"
        )

    if not raw.strip():
        raise ValueError("LLM returned empty content after extraction")

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[Ingest] JSON parse failed: {exc}", flush=True)
        print(f"[Ingest] Content that failed to parse: {raw[:500]!r}", flush=True)
        raise

    print(f"[Ingest] Parsed OK — sections={len(result.get('sections', []))}", flush=True)
    result["doc_name"] = doc_name
    result.setdefault("doc_summary", "")
    result.setdefault("sections", [])
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def count_propositions(ingestion_result: dict) -> int:
    return sum(
        len(s.get("propositions", []))
        for s in ingestion_result.get("sections", [])
    )
