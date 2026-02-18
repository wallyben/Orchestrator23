from adapters.anthropic_adapter import AnthropicAdapter
from adapters.base import LLMAdapter
from adapters.openai_adapter import OpenAIAdapter

ADAPTERS = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
}


def create_adapter(
    name: str,
    api_key: str = "",
    model: str = "",
    timeout: int = 120,
) -> LLMAdapter:
    cls = ADAPTERS.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown adapter: {name!r}. Available: {sorted(ADAPTERS)}"
        )
    return cls(api_key=api_key, model=model, timeout=timeout)
