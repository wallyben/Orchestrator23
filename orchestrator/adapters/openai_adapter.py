import os

from adapters.base import LLMAdapter


class OpenAIAdapter(LLMAdapter):
    def __init__(self, api_key: str = "", model: str = "", timeout: int = 120):
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._model = model or "gpt-4o"
        self._timeout = timeout
        self._client = None
        self._sdk = None
        try:
            import openai

            self._sdk = openai
            if self._api_key:
                self._client = openai.OpenAI(
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
                    "openai SDK not installed. Run: pip install openai"
                )
            raise RuntimeError(
                "OPENAI_API_KEY not set. Export it before running."
            )

        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        sdk = self._sdk
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=full_messages,
            )
        except sdk.APITimeoutError:
            raise RuntimeError(
                f"OpenAI API timed out after {self._timeout}s"
            )
        except sdk.APIConnectionError as e:
            raise RuntimeError(f"OpenAI API connection failed: {e}")
        except sdk.RateLimitError as e:
            raise RuntimeError(f"OpenAI API rate limited: {e}")
        except sdk.APIStatusError as e:
            raise RuntimeError(
                f"OpenAI API error (HTTP {e.status_code}): {e}"
            )
        except Exception as e:
            raise RuntimeError(f"Unexpected OpenAI API error: {e}")

        choice = response.choices[0] if response.choices else None
        if choice is None or choice.message is None:
            raise RuntimeError("OpenAI returned empty response")

        text = choice.message.content or ""
        usage = response.usage

        return {
            "text": text,
            "usage": {
                "input_tokens": usage.prompt_tokens if usage else 0,
                "output_tokens": usage.completion_tokens if usage else 0,
            },
            "stop_reason": choice.finish_reason or "unknown",
        }
