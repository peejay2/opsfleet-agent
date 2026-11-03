"""What happens when Gemini fails: retry with backoff, then a short message instead of a crash."""
import httpx
from google.adk.models import LlmResponse
from google.genai import errors, types

# Retries 408, 429 and 5xx responses, timeouts and dropped connections; waits 2s, 4s, ... up to 30s.
GEMINI_RETRIES = types.HttpRetryOptions(attempts=4, initial_delay=2.0, max_delay=30.0)


def gemini_failure_message(error: Exception) -> str:
    """Plain-English explanation of a Gemini error the retries didn't fix."""
    if isinstance(error, errors.APIError):
        if error.code == 429:
            return "Gemini's usage limit is reached for now. Please wait a minute and ask again."
        if error.code >= 500 or error.code == 408:
            return "Gemini is overloaded or down right now. Please try again in a few minutes."
        if error.code in (400, 401, 403, 404):
            return f"Gemini refused the request, which usually means a setup problem (API key or model name): {error.message}"
    if isinstance(error, (httpx.TimeoutException, httpx.ConnectError)):
        return "Gemini isn't responding. Check your internet connection, or try again in a few minutes."
    return f"Something went wrong talking to Gemini: {error}"


def on_model_error(callback_context, llm_request, error: Exception) -> LlmResponse:
    """Turn a failed Gemini call into a normal reply, so the chat carries on."""
    text = gemini_failure_message(error)
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
