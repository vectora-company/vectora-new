"""Adaptadores de API remota para Cloudflare Clef e JEV SystemOne."""

from __future__ import annotations

import logging
import time

import httpx

from backend.llm.systemone.endpoints import (
    CLEF_CLOUDFLARE_MODELS,
    JEV_API_BASE_URL,
    JEV_MODELS,
    OPENROUTER_API_BASE_URL,
)
from backend.llm.systemone.errors import (
    SystemOneAuthenticationError,
    SystemOneCapabilityError,
    SystemOneConnectionError,
    SystemOneRateLimitError,
    SystemOneTimeoutError,
    SystemOneValidationError,
)
from backend.llm.systemone.protocol import SystemOneProvider
from backend.llm.systemone.types import (
    ChoiceQuestion,
    NoulQuestion,
    ProviderCapabilities,
    ScoreQuestion,
    SystemOneRequest,
    SystemOneResponse,
    parse_systemone_response,
)
from backend.vtypes.contracts import JsonObject, JsonValue

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30.0


class BaseSystemOneApiAdapter(SystemOneProvider):
    """Base compartilhada para adapters de API HTTP SystemOne (JEV e Clef API)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        provider_name: str,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Inicializa o estado da instância."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._timeout = timeout
        self._external_client = client

    @property
    def provider_id(self) -> str:
        """Executa a operação provider id."""
        return self._provider_name

    async def aclose(self) -> None:
        """Encerra recursos de transporte HTTP quando gerenciados pelo adapter."""
        return

    async def health(self) -> bool:
        """Executa a operação health."""
        if not self._api_key:
            return False
        url = f"{self._base_url}/health"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._external_client is not None:
                res = await self._external_client.get(url, headers=headers)
                if res.status_code in (401, 403):
                    raise SystemOneAuthenticationError(
                        "credencial rejeitada no health probe",
                        provider=self.provider_id,
                    )
                return res.status_code == 200
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(url, headers=headers)
                if res.status_code in (401, 403):
                    raise SystemOneAuthenticationError(
                        "credencial rejeitada no health probe",
                        provider=self.provider_id,
                    )
                return res.status_code == 200
        except SystemOneAuthenticationError:
            raise
        except Exception:
            return False

    def _decide_endpoint(self, model: str) -> str:
        """Executa a operação decide endpoint."""
        return f"{self._base_url}/v1/systemone"

    async def decide(self, request: SystemOneRequest) -> SystemOneResponse:
        """Executa a operação decide."""
        if not self._api_key or not self._api_key.strip():
            raise SystemOneAuthenticationError(
                f"Chave de API necessária para {self.provider_id}",
                provider=self.provider_id,
            )

        caps = await self.capabilities()
        if request.media_urls and not (caps.supports_image or caps.supports_video):
            raise SystemOneCapabilityError(
                f"{self.provider_id} não suporta mídias na configuração atual.",
                provider=self.provider_id,
            )

        endpoint = self._decide_endpoint(request.model)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        serialized_questions: JsonObject = {}
        for q_id, q in request.questions.items():
            if isinstance(q, NoulQuestion):
                serialized_questions[q_id] = {
                    "type": "noul",
                    "criterion": q.criterion,
                    "instructions": q.instructions,
                }
            elif isinstance(q, ChoiceQuestion):
                serialized_questions[q_id] = {
                    "type": "choice",
                    "options": [*q.options],
                    "instructions": q.instructions,
                }
            elif isinstance(q, ScoreQuestion):
                serialized_questions[q_id] = {
                    "type": "score",
                    "criterion": q.criterion,
                    "min_score": q.min_score,
                    "max_score": q.max_score,
                    "instructions": q.instructions,
                }

        payload: JsonObject = {
            "model": request.model,
            "state": request.state,
            "questions": serialized_questions,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.media_urls:
            payload["media_urls"] = [*request.media_urls]

        start_time = time.perf_counter()
        try:
            if self._external_client is not None:
                res = await self._external_client.post(
                    endpoint, json=payload, headers=headers
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    res = await client.post(endpoint, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise SystemOneTimeoutError(
                f"Timeout ({self._timeout}s) em {self.provider_id}",
                provider=self.provider_id,
            ) from exc
        except httpx.RequestError as exc:
            raise SystemOneConnectionError(
                "systemone_connection_error",
                provider=self.provider_id,
            ) from exc

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        if res.status_code in (401, 403):
            raise SystemOneAuthenticationError(
                f"Autenticação rejeitada por {self.provider_id} (HTTP {res.status_code})",
                provider=self.provider_id,
            )
        if res.status_code == 429:
            retry_after = res.headers.get("Retry-After")
            retry_s = (
                float(retry_after) if retry_after and retry_after.isdigit() else None
            )
            raise SystemOneRateLimitError(
                f"Limite de requisições excedido em {self.provider_id}",
                provider=self.provider_id,
                retry_after=retry_s,
            )
        if res.status_code >= 500:
            raise SystemOneConnectionError(
                f"Erro HTTP {res.status_code} de {self.provider_id}",
                provider=self.provider_id,
            )
        if res.status_code >= 400:
            raise SystemOneValidationError(
                f"Erro HTTP {res.status_code} de {self.provider_id}",
                provider=self.provider_id,
            )

        try:
            data = res.json()
        except Exception as exc:
            raise SystemOneValidationError(
                f"Resposta de {self.provider_id} não é JSON válido",
                provider=self.provider_id,
            ) from exc

        if (
            self.provider_id == "clef-api"
            and isinstance(data, dict)
            and "success" in data
        ):
            if data.get("success") is not True or not isinstance(
                data.get("result"), dict
            ):
                raise SystemOneValidationError(
                    "Envelope REST do Cloudflare indica falha ou resultado inválido",
                    provider=self.provider_id,
                )
            data = data["result"]

        return parse_systemone_response(
            data,
            request,
            provider=self.provider_id,
            latency_ms=elapsed_ms,
        )


class ClefApiAdapter(BaseSystemOneApiAdapter):
    """Adaptador para Clef via API remota (Cloudflare Workers AI / Clef Cloud)."""

    def __init__(
        self,
        api_key: str,
        account_id: str,
        base_url: str = "https://api.cloudflare.com/client/v4/accounts",
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Inicializa o estado da instância."""
        if not account_id or not account_id.strip():
            raise ValueError("account_id é obrigatório para ClefApiAdapter")
        self._account_id = account_id.strip()
        super().__init__(
            base_url,
            api_key,
            provider_name="clef-api",
            timeout=timeout,
            client=client,
        )

    async def health(self) -> bool:
        """Executa a operação health."""
        if not self._api_key:
            return False
        url = "https://api.cloudflare.com/client/v4/user/tokens/verify"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._external_client is not None:
                res = await self._external_client.get(url, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    res = await client.get(url, headers=headers)
            if res.status_code in (401, 403):
                raise SystemOneAuthenticationError(
                    "credencial rejeitada no health probe", provider=self.provider_id
                )
            return res.status_code == 200
        except SystemOneAuthenticationError:
            raise
        except Exception:
            return False

    def _decide_endpoint(self, model: str) -> str:
        """Executa a operação decide endpoint."""
        suffix = f"/{self._account_id}"
        if self._base_url.endswith(suffix):
            return f"{self._base_url}/ai/run/{model}"
        return f"{self._base_url}/{self._account_id}/ai/run/{model}"

    async def capabilities(self) -> ProviderCapabilities:
        """Executa a operação capabilities."""
        return ProviderCapabilities(
            supports_noul=True,
            supports_choice=True,
            supports_score=True,
            supports_image=True,
            supports_video=False,
            supported_models=list(CLEF_CLOUDFLARE_MODELS),
            evidence="documented",
        )


class JevApiAdapter(BaseSystemOneApiAdapter):
    """Adaptador para JEV via API remota (JEV Cloud)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = JEV_API_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Inicializa o estado da instância."""
        super().__init__(
            base_url,
            api_key,
            provider_name="jev-api",
            timeout=timeout,
            client=client,
        )

    async def capabilities(self) -> ProviderCapabilities:
        """Executa a operação capabilities."""
        return ProviderCapabilities(
            supports_noul=True,
            supports_choice=True,
            supports_score=True,
            supports_image=True,
            supports_video=False,
            supported_models=list(JEV_MODELS),
            evidence="documented",
        )


#: Máximo de níveis ordinais enviados numa pergunta de score ao OpenRouter.
_OPENROUTER_MAX_SCORE_LEVELS = 11


def _openrouter_score_levels(question: ScoreQuestion) -> list[str]:
    """Níveis ordinais de uma pergunta de score (o OpenRouter pontua 0..N-1)."""
    if question.levels:
        return list(question.levels)
    steps = int(question.max_score - question.min_score)
    count = max(2, min(steps + 1, _OPENROUTER_MAX_SCORE_LEVELS))
    return [
        f"{question.criterion} (nível {index + 1} de {count})" for index in range(count)
    ]


def _normalized_distribution(value: JsonValue) -> JsonValue:
    """Reajusta o arredondamento de 4 casas do OpenRouter para somar 1.0."""
    if not isinstance(value, dict) or not value:
        return value
    numbers: dict[str, float] = {}
    for key, raw in value.items():
        if not isinstance(raw, int | float | str):
            return value
        try:
            numbers[key] = float(raw)
        except ValueError:
            return value
    total = sum(numbers.values())
    if total > 0 and abs(total - 1.0) <= 0.01:
        return {key: probability / total for key, probability in numbers.items()}
    return value


class OpenRouterDecisionAdapter(SystemOneProvider):
    """Decision Models pela API de Decisions do OpenRouter.

    Um único adaptador serve todos os modelos de decisão do OpenRouter (JEV,
    Clef, Gemini Decision, GPT-6 Luna Decisions, Perplexity Decider, Liquid d1,
    entre outros). Perguntas e respostas são convertidas entre o formato
    canônico do Vectora e o do OpenRouter; recusas e respostas fora do contrato
    viram erro, nunca uma resposta inventada.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = OPENROUTER_API_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Inicializa o estado da instância."""
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._external_client = client

    @property
    def provider_id(self) -> str:
        """Executa a operação provider id."""
        return "openrouter-api"

    async def capabilities(self) -> ProviderCapabilities:
        """Executa a operação capabilities."""
        return ProviderCapabilities(
            supports_noul=True,
            supports_choice=True,
            supports_score=True,
            supports_image=True,
            supports_video=False,
            supported_models=[],
            evidence="documented",
        )

    async def health(self) -> bool:
        """Confere a chave no endpoint de informações da chave do OpenRouter."""
        if not self._api_key:
            return False
        url = f"{self._base_url}/v1/key"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._external_client is not None:
                res = await self._external_client.get(url, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    res = await client.get(url, headers=headers)
        except Exception:
            return False
        if res.status_code in (401, 403):
            raise SystemOneAuthenticationError(
                "credencial rejeitada no health probe", provider=self.provider_id
            )
        return res.status_code == 200

    @staticmethod
    def _serialize_questions(request: SystemOneRequest) -> JsonObject:
        serialized: JsonObject = {}
        for q_id, question in request.questions.items():
            spec: JsonObject
            if isinstance(question, NoulQuestion):
                spec = {
                    "type": "noul",
                    "instructions": "\n".join(
                        part
                        for part in (question.criterion, question.instructions)
                        if part
                    ),
                }
                if question.criteria:
                    spec["criteria"] = {**question.criteria}
            elif isinstance(question, ChoiceQuestion):
                spec = {
                    "type": "choice",
                    "instructions": question.instructions or "",
                    "criteria": {**question.criteria}
                    if question.criteria
                    else {option: option for option in question.options},
                }
            elif isinstance(question, ScoreQuestion):
                spec = {
                    "type": "score",
                    "instructions": "\n".join(
                        part
                        for part in (question.criterion, question.instructions)
                        if part
                    ),
                    "criteria": [*_openrouter_score_levels(question)],
                }
            else:  # pragma: no cover - união fechada
                continue
            serialized[q_id] = spec
        return serialized

    def _translate_answers(
        self, data: JsonObject, request: SystemOneRequest
    ) -> JsonObject:
        """Converte as respostas do OpenRouter para o envelope canônico."""
        raw_answers = data.get("answers")
        if not isinstance(raw_answers, dict):
            raise SystemOneValidationError(
                "systemone_invalid_response", provider=self.provider_id
            )
        canonical: JsonObject = {}
        for q_id, question in request.questions.items():
            answer = raw_answers.get(q_id)
            if not isinstance(answer, dict) or answer.get("type") == "refusal":
                # Fail closed: pergunta recusada ou ausente não vira decisão.
                raise SystemOneCapabilityError(
                    f"{self.provider_id} não respondeu à pergunta {q_id!r}.",
                    provider=self.provider_id,
                )
            kind = answer.get("type")
            confidence = answer.get("confidence")
            if kind == "noul":
                canonical[q_id] = {
                    "type": "noul",
                    "probability": answer.get("noul"),
                    "confidence": confidence,
                }
            elif kind == "choice":
                canonical[q_id] = {
                    "type": "choice",
                    "selection": answer.get("choice"),
                    "distribution": _normalized_distribution(
                        answer.get("probabilities")
                    ),
                    "confidence": confidence,
                }
            elif kind == "score" and isinstance(question, ScoreQuestion):
                levels = len(_openrouter_score_levels(question))
                raw_score = answer.get("score")
                if not isinstance(raw_score, int | float) or levels < 2:
                    raise SystemOneValidationError(
                        "systemone_invalid_response", provider=self.provider_id
                    )
                fraction = min(max(raw_score / (levels - 1), 0.0), 1.0)
                canonical[q_id] = {
                    "type": "score",
                    "score": question.min_score
                    + fraction * (question.max_score - question.min_score),
                    "min_score": question.min_score,
                    "max_score": question.max_score,
                    "confidence": confidence,
                }
            else:
                canonical[q_id] = {"type": kind}
        return canonical

    async def decide(self, request: SystemOneRequest) -> SystemOneResponse:
        """Executa a operação decide."""
        if not self._api_key or not self._api_key.strip():
            raise SystemOneAuthenticationError(
                "Chave de API do OpenRouter necessária",
                provider=self.provider_id,
            )
        if request.media_urls:
            raise SystemOneCapabilityError(
                "Mídias por URL ainda não são enviadas ao OpenRouter Decisions.",
                provider=self.provider_id,
            )

        payload = {
            "model": request.model,
            "state": request.state,
            "questions": self._serialize_questions(request),
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        endpoint = f"{self._base_url}/alpha/decisions"
        start_time = time.perf_counter()
        try:
            if self._external_client is not None:
                res = await self._external_client.post(
                    endpoint, json=payload, headers=headers
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    res = await client.post(endpoint, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise SystemOneTimeoutError(
                f"Timeout ({self._timeout}s) em {self.provider_id}",
                provider=self.provider_id,
            ) from exc
        except httpx.RequestError as exc:
            raise SystemOneConnectionError(
                "systemone_connection_error", provider=self.provider_id
            ) from exc
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        if res.status_code in (401, 402, 403):
            raise SystemOneAuthenticationError(
                f"Autenticação ou crédito rejeitado por {self.provider_id} "
                f"(HTTP {res.status_code})",
                provider=self.provider_id,
            )
        if res.status_code == 429:
            retry_after = res.headers.get("Retry-After")
            raise SystemOneRateLimitError(
                f"Limite de requisições excedido em {self.provider_id}",
                provider=self.provider_id,
                retry_after=float(retry_after)
                if retry_after and retry_after.isdigit()
                else None,
            )
        if res.status_code >= 500:
            raise SystemOneConnectionError(
                f"Erro HTTP {res.status_code} de {self.provider_id}",
                provider=self.provider_id,
            )
        if res.status_code >= 400:
            raise SystemOneValidationError(
                f"Erro HTTP {res.status_code} de {self.provider_id}",
                provider=self.provider_id,
            )
        try:
            data = res.json()
        except Exception as exc:
            raise SystemOneValidationError(
                f"Resposta de {self.provider_id} não é JSON válido",
                provider=self.provider_id,
            ) from exc
        if not isinstance(data, dict):
            raise SystemOneValidationError(
                "systemone_invalid_response", provider=self.provider_id
            )

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        envelope: JsonObject = {
            "answers": self._translate_answers(data, request),
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        }
        return parse_systemone_response(
            envelope, request, provider=self.provider_id, latency_ms=elapsed_ms
        )
