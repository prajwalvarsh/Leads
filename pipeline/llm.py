"""OpenRouter client: structured JSON output (json_schema, strict) with call caps and a disk cache.

The model, prompt version and page text form the cache key, so reruns cost nothing.
Budget units are reserved in the calling thread (DuckDB connection is not shared across threads).
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from openai import OpenAI

from pipeline.budget import Budget, CapExceeded
from pipeline.config import CACHE, ROOT, env

LLM_CACHE = CACHE / "llm"
BASE_URL = "https://openrouter.ai/api/v1"


def load_prompt(role: str, version: int) -> str:
    return (ROOT / "prompts" / f"{role}_v{version}.md").read_text(encoding="utf-8")


class LLM:
    def __init__(self, model: str, budget: Budget, temperature: float | None = 0, max_retries: int = 3) -> None:
        """temperature=None: not sent (some models, e.g. claude-haiku-5.5, do not accept it)."""
        key = env("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY missing in .env")
        self.client = OpenAI(base_url=BASE_URL, api_key=key, timeout=90, max_retries=max_retries)
        self.model = model
        self.budget = budget
        self.temperature = temperature
        self.calls = 0
        self.cache_hits = 0
        self.tokens_in = 0
        self.tokens_out = 0
        LLM_CACHE.mkdir(parents=True, exist_ok=True)

    def _path(self, system: str, user: str, schema: dict) -> Path:
        raw = json.dumps([self.model, system, user, schema], sort_keys=True, ensure_ascii=False)
        return LLM_CACHE / f"{hashlib.sha1(raw.encode()).hexdigest()}.json"

    def _call(self, system: str, user: str, name: str, schema: dict) -> dict:
        extra = {} if self.temperature is None else {"temperature": self.temperature}
        r = self.client.chat.completions.create(
            model=self.model,
            **extra,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
            extra_body={"provider": {"require_parameters": True}},
        )
        out = json.loads(r.choices[0].message.content or "{}")
        usage = r.usage
        return {"output": out, "tokens_in": usage.prompt_tokens if usage else 0,
                "tokens_out": usage.completion_tokens if usage else 0}

    def run_many(self, system: str, users: list[str], name: str, schema: dict,
                 concurrency: int = 8) -> list[dict | Exception]:
        """One structured call per user message. Cached results are free; others reserve a budget
        unit before the call. Raises CapExceeded before any call that would exceed the cap."""
        results: list[dict | Exception | None] = [None] * len(users)
        todo: list[int] = []
        for i, u in enumerate(users):
            p = self._path(system, u, schema)
            if p.exists():
                results[i] = json.loads(p.read_text(encoding="utf-8"))["output"]
                self.cache_hits += 1
            else:
                todo.append(i)
        if len(todo) > self.budget.remaining():
            raise CapExceeded(f"{self.budget.api}: {len(todo)} calls needed, {self.budget.remaining()} left under the cap")
        for _ in todo:
            self.budget.spend(1)

        def work(i: int) -> tuple[int, dict | Exception]:
            try:
                res = self._call(system, users[i], name, schema)
            except Exception as e:  # one failed call must not stop the stage
                return i, e
            self._path(system, users[i], schema).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
            return i, res

        with ThreadPoolExecutor(concurrency) as pool:
            for i, res in pool.map(work, todo):
                if isinstance(res, Exception):
                    results[i] = res
                else:
                    self.calls += 1
                    self.tokens_in += res["tokens_in"]
                    self.tokens_out += res["tokens_out"]
                    results[i] = res["output"]
        return results  # type: ignore[return-value]
