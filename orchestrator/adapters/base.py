class LLMAdapter:
    def generate(
        self,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 16000,
    ) -> dict:
        """
        Returns: {"text": str, "usage": {"input_tokens": int, "output_tokens": int}, "stop_reason": str}
        Raises RuntimeError on failure.
        """
        raise NotImplementedError
