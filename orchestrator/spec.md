# Example Spec: URL Shortener Library

## Overview

Build a Python URL shortener library with the following capabilities:

## Requirements

1. **`shorten(url: str) -> str`** — Takes a full URL and returns a short 8-character alphanumeric code.
2. **`expand(code: str) -> str | None`** — Takes a short code and returns the original URL, or `None` if not found.
3. **Storage** — Use an in-memory dictionary. No database.
4. **Deterministic** — The same URL must always produce the same short code.
5. **Collision handling** — If two different URLs hash to the same code, append an incrementing suffix.
6. **Validation** — `shorten()` must raise `ValueError` if the input is not a valid URL (must start with `http://` or `https://`).

## Constraints

- Pure Python, no external dependencies.
- Single file: `shortener.py`
- Test file: `test_shortener.py`

## Test Cases Expected

- Shorten a valid URL and get an 8-char code
- Expand a code back to the original URL
- Same URL returns the same code (deterministic)
- Invalid URL raises ValueError
- Expand unknown code returns None
