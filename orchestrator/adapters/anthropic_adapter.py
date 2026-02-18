import os

from adapters.base import LLMAdapter

_STUB_RESPONSE = {
    "text": "",
    "usage": {"input_tokens": 0, "output_tokens": 0},
    "stop_reason": "stub",
}


class AnthropicAdapter(LLMAdapter):
    def __init__(self, api_key: str = "", model: str = "", timeout: int = 120):
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = model or "claude-sonnet-4-20250514"
        self._timeout = timeout
        self._client = None
        self._sdk = None
        try:
            import anthropic

            self._sdk = anthropic
            if self._api_key:
                self._client = anthropic.Anthropic(
                    api_key=self._api_key, timeout=float(timeout)
                )
        except ImportError:
            pass

    def generate(
        self,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 16000,
    ) -> dict:
        if self._client is None:
            if self._sdk is None:
                raise RuntimeError(
                    "anthropic SDK not installed. Run: pip install anthropic"
                )
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Export it before running."
            )

        sdk = self._sdk
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            )
        except sdk.APITimeoutError:
            raise RuntimeError(
                f"Anthropic API timed out after {self._timeout}s"
            )
        except sdk.APIConnectionError as e:
            raise RuntimeError(f"Anthropic API connection failed: {e}")
        except sdk.RateLimitError as e:
            raise RuntimeError(f"Anthropic API rate limited: {e}")
        except sdk.APIStatusError as e:
            raise RuntimeError(
                f"Anthropic API error (HTTP {e.status_code}): {e}"
            )
        except Exception as e:
            raise RuntimeError(f"Unexpected Anthropic API error: {e}")

        if not response.content:
            raise RuntimeError("Anthropic returned empty response")

        text = ""
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                text += block.text

        return {
            "text": text,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            "stop_reason": response.stop_reason,
        }
