"""
LLM client for the enrichment step — provider chosen by env.

    LLM_PROVIDER   "gemini" (default) or "openai" (any OpenAI-compatible API)
    LLM_API_KEY    without it get_llm_client() returns None and enrichment is skipped
    LLM_MODEL      model id for the chosen provider
    LLM_BASE_URL   optional; override the endpoint (self-hosted or proxy gateways)

Both providers are called over plain HTTPS with `requests`, so switching
provider does not change the dependency set. The client asks for a JSON
response and returns the raw text; parsing and validation live in enrichment.py.
"""

import requests

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PROVIDER, LLM_TIMEOUT

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_BASE_URL = "https://api.openai.com/v1"
SUPPORTED_PROVIDERS = ("gemini", "openai")

# Low temperature: the task is cleanup and extraction, not creative writing.
TEMPERATURE = 0.2


class LLMError(Exception):
    """Transport or provider failure. The RQ job lets it propagate so the job is retried."""


class LLMClient:
    def __init__(self, provider: str, api_key: str, model: str,
                 base_url: str = "", timeout: int = 120):
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported LLM_PROVIDER: {provider!r}")
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        default_base = GEMINI_BASE_URL if provider == "gemini" else OPENAI_BASE_URL
        self.base_url = (base_url or default_base).rstrip("/")

    def complete_json(self, system: str, user: str) -> str:
        """
        One completion with a JSON response requested.
        Returns the raw response text, or "" when the provider returned no
        content (for example a safety block). Raises LLMError on HTTP errors.
        """
        if self.provider == "gemini":
            return self._gemini(system, user)
        return self._openai(system, user)

    # -- providers ----------------------------------------------------------

    def _gemini(self, system: str, user: str) -> str:
        data = self._post(
            f"{self.base_url}/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self.api_key},
            payload={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": TEMPERATURE,
                },
            },
        )
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(part.get("text", "") for part in parts)

    def _openai(self, system: str, user: str) -> str:
        data = self._post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            payload={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "temperature": TEMPERATURE,
            },
        )
        choices = data.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message") or {}).get("content") or ""

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise LLMError(f"{self.provider} request failed: {type(e).__name__}") from e
        if response.status_code != 200:
            # Body is truncated; the API key travels in headers and is never logged.
            raise LLMError(f"{self.provider} returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as e:
            raise LLMError(f"{self.provider} returned a non-JSON body") from e


def llm_configured() -> bool:
    return bool(LLM_API_KEY and LLM_MODEL and LLM_PROVIDER in SUPPORTED_PROVIDERS)


def get_llm_client():
    """Client built from env, or None when enrichment is not configured."""
    if not llm_configured():
        return None
    return LLMClient(LLM_PROVIDER, LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, LLM_TIMEOUT)
