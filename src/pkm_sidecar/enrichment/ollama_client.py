"""Async client for the enrichment model host (docs/enrichment.md §3, §7).

**This client POSTs, and that is fine.** The read-only guarantee this project
makes is about *Joplin*: `joplin_client.py` exposes only `get_*`, an AST scan
keeps it that way, and its transport is fenced to the Joplin origin. None of that
is weakened by a different module talking to a different host — Ollama's
generate and embed endpoints simply require POST.

The fence is per-client, so this one gets its own
:class:`~pkm_sidecar.security.SingleOriginTransport` pinned to the configured
Ollama base. A second permitted origin is a second transport; the Joplin
client's allowlist does not grow.

Behaviour here is shaped by measurements against a real host rather than
guesses — see :meth:`OllamaClient.generate` on reasoning models, which return an
empty string unless their thinking budget is accounted for.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Any

import httpx

from pkm_sidecar import security
from pkm_sidecar.errors import (
    OllamaBadResponseError,
    OllamaError,
    OllamaModelNotFoundError,
    OllamaUnreachableError,
)
from pkm_sidecar.logging_config import LogOnceGuard, get_logger, retry_with_backoff

# Generation on CPU or an iGPU is slow by design — the measured 8B model runs at
# roughly 1s per short title, and a long note costs more in prompt evaluation.
# This is a background batch, so the timeout is generous rather than snappy.
DEFAULT_TIMEOUT = 120.0
EMBED_TIMEOUT = 60.0
# Reachability probes must not inherit the generation timeout. A host that
# accepts the connection and then never answers would otherwise leave the
# interactive `doctor` command looking hung for two minutes while it asks a
# question that should take milliseconds.
PROBE_TIMEOUT = 10.0

_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


class GenerateResult:
    """A completion, keeping ``thinking`` distinct from the answer.

    Reasoning models put their working in a separate channel and the answer in
    ``response``. Measured on a real host: ``gpt-oss:20b`` under a short
    ``num_predict`` returns an **empty** ``response`` with a full ``thinking``,
    because the budget was spent before it reached an answer — and ``think:
    false`` does not suppress that. Collapsing the two fields would turn that
    into a silent empty title; keeping them apart lets the caller detect it.
    """

    __slots__ = ("model", "response", "thinking", "truncated")

    def __init__(self, response: str, thinking: str, model: str, *, truncated: bool) -> None:
        self.response = response
        self.thinking = thinking
        self.model = model
        self.truncated = truncated

    @property
    def spent_budget_thinking(self) -> bool:
        """Empty answer, non-empty working: the reasoning-model trap above."""
        return not self.response.strip() and bool(self.thinking.strip())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"GenerateResult(model={self.model!r}, response={self.response[:40]!r})"


class OllamaClient:
    """Async client for Ollama's generate/embed API, fenced to one origin."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 1,
        backoff_initial: float = 0.5,
        user_agent: str = "pkm-sidecar/enrichment",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._backoff_initial = backoff_initial
        self._log = get_logger("ollama_client")
        self._ping_guard = LogOnceGuard()
        guarded = security.SingleOriginTransport(self._base_url, transport)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout, connect=5.0),
            headers={"User-Agent": user_agent},
            transport=guarded,
        )

    # --- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OllamaClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # --- internals ---------------------------------------------------------

    def _check_status(self, resp: httpx.Response, *, model: str | None = None) -> None:
        code = resp.status_code
        if 200 <= code < 300:
            return
        url = str(resp.request.url)
        if code == 404:
            raise OllamaModelNotFoundError(
                f"Model {model!r} is not available on the enrichment host. "
                f"Pull it there first (`ollama pull {model}`)."
                if model
                else "Ollama endpoint not found.",
                url=url,
                status_code=code,
            )
        raise OllamaBadResponseError(
            f"Unexpected status {code} from the enrichment host.", url=url, status_code=code
        )

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        async def _do() -> httpx.Response:
            return await self._client.post(
                path,
                json=payload,
                timeout=httpx.Timeout(timeout, connect=5.0)
                if timeout is not None
                else httpx.USE_CLIENT_DEFAULT,
            )

        try:
            resp = await retry_with_backoff(
                _do,
                retries=self._max_retries,
                base_delay=self._backoff_initial,
                retry_on=_TRANSIENT_ERRORS,
                logger=self._log,
                op=f"POST {path}",
            )
        except _TRANSIENT_ERRORS as exc:
            raise OllamaUnreachableError(
                f"Could not reach the enrichment host: {type(exc).__name__}",
                url=f"{self._base_url}{path}",
            ) from exc
        self._check_status(resp, model=model)
        try:
            data = resp.json()
        except ValueError as exc:
            raise OllamaBadResponseError(
                "Enrichment host returned a non-JSON body.", url=str(resp.request.url)
            ) from exc
        # Annotating this as a dict would not make it one: a JSON array or string
        # root would sail through and only fail later as an AttributeError from
        # .get(), which is not an OllamaError and so escapes every caller's
        # handling.
        if not isinstance(data, dict):
            raise OllamaBadResponseError(
                f"Expected a JSON object from {path}, got {type(data).__name__}.",
                url=str(resp.request.url),
            )
        result: dict[str, Any] = data
        return result

    # --- public API --------------------------------------------------------

    async def ping(self) -> bool:
        """True if the host answers; never raises, so callers can degrade quietly."""
        try:
            resp = await self._client.get(
                "/api/tags", timeout=httpx.Timeout(PROBE_TIMEOUT, connect=5.0)
            )
            return 200 <= resp.status_code < 300
        except (httpx.HTTPError, OllamaError) as exc:
            if self._ping_guard.should_log("ollama.unreachable"):
                self._log.warning("Enrichment host ping failed: %s", type(exc).__name__)
            return False

    async def list_models(self) -> list[str]:
        """Model names present on the host, for the doctor check.

        Every failure here has to arrive as an :class:`OllamaError`. Point the
        base URL at a generic web server or a misconfigured proxy and the reply is
        a 200 carrying HTML, or JSON that is not an object — and ``doctor`` only
        catches ``OllamaError``, so an unwrapped ``ValueError`` would abort the
        whole run with a traceback instead of reporting one failed check.
        """
        try:
            resp = await self._client.get(
                "/api/tags", timeout=httpx.Timeout(PROBE_TIMEOUT, connect=5.0)
            )
        except _TRANSIENT_ERRORS as exc:
            raise OllamaUnreachableError(
                f"Could not reach the enrichment host: {type(exc).__name__}",
                url=f"{self._base_url}/api/tags",
            ) from exc
        self._check_status(resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise OllamaBadResponseError(
                "Enrichment host returned a non-JSON model list — is the base URL "
                "pointing at Ollama?",
                url=str(resp.request.url),
            ) from exc
        if not isinstance(data, dict):
            raise OllamaBadResponseError(
                f"Expected a JSON object from /api/tags, got {type(data).__name__}.",
                url=str(resp.request.url),
            )
        models = data.get("models", [])
        if not isinstance(models, list):
            raise OllamaBadResponseError(
                f"Expected a list of models, got {type(models).__name__}.",
                url=str(resp.request.url),
            )
        return [m["name"] for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)]

    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        num_predict: int = 32,
        temperature: float = 0.0,
        timeout: float | None = None,
        options: dict[str, Any] | None = None,
    ) -> GenerateResult:
        """Run a single non-streaming completion.

        ``temperature`` defaults to 0: a 1.5B model was measured giving a
        different — and wrong — answer for the same note across two runs at 0.2,
        and suggestions are cached per identity, so a re-roll is not wanted.
        """
        opts: dict[str, Any] = {"temperature": temperature, "num_predict": num_predict}
        opts.update(options or {})
        data = await self._post(
            "/api/generate",
            {"model": model, "prompt": prompt, "stream": False, "options": opts},
            timeout=timeout,
            model=model,
        )
        result = GenerateResult(
            response=str(data.get("response") or ""),
            thinking=str(data.get("thinking") or ""),
            model=model,
            truncated=data.get("done_reason") == "length",
        )
        if result.spent_budget_thinking:
            self._log.warning(
                "Model %s returned no answer within num_predict=%d — its whole budget "
                "went on reasoning. Raise num_predict or choose a non-reasoning model.",
                model,
                num_predict,
            )
        return result

    async def embed(
        self, model: str, inputs: Sequence[str], *, timeout: float | None = EMBED_TIMEOUT
    ) -> list[list[float]]:
        """Embed a batch of texts. Measured at ~30ms/doc batched for bge-large."""
        if not inputs:
            return []
        data = await self._post(
            "/api/embed", {"model": model, "input": list(inputs)}, timeout=timeout, model=model
        )
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(inputs):
            raise OllamaBadResponseError(
                f"Expected {len(inputs)} embeddings, got "
                f"{len(vectors) if isinstance(vectors, list) else type(vectors).__name__}."
            )
        # Shape as well as count. A string where a vector should be would iterate
        # into characters and fail in float() as a ValueError, which no caller
        # expects; a corrupt vector must surface as an OllamaError like anything
        # else from this host.
        out: list[list[float]] = []
        for index, vector in enumerate(vectors):
            if not isinstance(vector, list):
                raise OllamaBadResponseError(
                    f"Embedding {index} is {type(vector).__name__}, expected a list."
                )
            try:
                out.append([float(x) for x in vector])
            except (TypeError, ValueError) as exc:
                raise OllamaBadResponseError(
                    f"Embedding {index} contains a non-numeric value."
                ) from exc
        return out
