"""Helpers de erro e fallback de capability.

Contrato:
- is_quota_error: reconhece 429/quota/rate-limit; ignora outros erros.
- get_fallback_chain: fallback global de chat permanece desabilitado.
- try_with_fallback: testa o contrato interno usado por adapters de capability.
- record_switch/drain_switches: fila por ContextVar, drena e limpa.
"""

from __future__ import annotations

from typing import Any, NoReturn
from unittest.mock import patch

import pytest

from backend.llm import provider_fallback as pf

# ---------------------------------------------------------------------------
# is_quota_error
# ---------------------------------------------------------------------------


class TestIsQuotaError:
    def test_429(self) -> None:
        assert pf.is_quota_error(Exception("429 RESOURCE_EXHAUSTED")) is True

    def test_resource_exhausted(self) -> None:
        assert pf.is_quota_error(Exception("RESOURCE_EXHAUSTED")) is True

    def test_quota_word(self) -> None:
        assert pf.is_quota_error(Exception("You exceeded your current quota")) is True

    def test_rate_limit_space(self) -> None:
        assert pf.is_quota_error(Exception("Rate limit reached")) is True

    def test_rate_limit_underscore(self) -> None:
        assert pf.is_quota_error(Exception("rate_limit_exceeded")) is True

    def test_case_insensitive(self) -> None:
        assert pf.is_quota_error(Exception("Quota Exceeded")) is True

    def test_non_quota_connection(self) -> None:
        assert pf.is_quota_error(Exception("connection refused")) is False

    def test_non_quota_value_error(self) -> None:
        assert pf.is_quota_error(ValueError("bad input")) is False

    def test_openrouter_rate_limit_error_reconhecida_por_tipo(self) -> None:
        """Reconhece por `isinstance`, não por substring — mensagem sem
        nenhum marker de `_QUOTA_MARKERS` ainda é detectada como quota."""
        from backend.llm.openrouter.client import OpenRouterRateLimitError

        exc = OpenRouterRateLimitError("too many requests, slow down")
        assert pf.is_quota_error(exc) is True

    def test_generic_exception_com_texto_de_rate_limit_ainda_funciona(self) -> None:
        """Regressão: exceção não tipada (Anthropic/Gemini/Cohere levantam a
        exceção genérica da própria SDK) continua reconhecida via substring."""
        assert pf.is_quota_error(Exception("429 rate limit exceeded")) is True

    def test_empty(self) -> None:
        assert pf.is_quota_error(Exception("")) is False

    def test_quota_exhausted_error_is_quota(self) -> None:
        assert pf.is_quota_error(pf.QuotaExhaustedError("gemini esgotou")) is True


# ---------------------------------------------------------------------------
# is_transient_error
# ---------------------------------------------------------------------------


class TestIsTransientError:
    def test_timeout_word(self) -> None:
        assert pf.is_transient_error(Exception("ReadTimeout: timed out")) is True

    def test_timed_out(self) -> None:
        assert pf.is_transient_error(Exception("request timed out after 30s")) is True

    def test_connecttimeout(self) -> None:
        assert (
            pf.is_transient_error(Exception("ConnectTimeout connecting to API")) is True
        )

    def test_readtimeout(self) -> None:
        assert pf.is_transient_error(Exception("ReadTimeout reading response")) is True

    def test_connection_error(self) -> None:
        assert (
            pf.is_transient_error(Exception("connection error: reset by peer")) is True
        )

    def test_connection_refused(self) -> None:
        assert pf.is_transient_error(Exception("connection refused")) is True

    def test_case_insensitive(self) -> None:
        assert pf.is_transient_error(Exception("TIMEOUT waiting for model")) is True

    def test_gemini_503_high_demand_real(self) -> None:
        # Texto real de um ServerError do google-genai (visto em produção).
        msg = (
            "503 Service Unavailable. {'message': '{\\n  \"error\": {\\n    "
            '"code": 503,\\n    "message": "This model is currently '
            "experiencing high demand. Spikes in demand are usually "
            'temporary. Please try again later.",\\n    "status": '
            "\"UNAVAILABLE\"\\n  }\\n}\\n', 'status': 'Service Unavailable'}"
        )
        assert pf.is_transient_error(Exception(msg)) is True

    def test_anthropic_overloaded(self) -> None:
        assert pf.is_transient_error(Exception("overloaded_error: overloaded")) is True

    def test_service_unavailable(self) -> None:
        assert pf.is_transient_error(Exception("503 service unavailable")) is True

    # erros que NÃO são transientes
    def test_quota_not_transient(self) -> None:
        assert pf.is_transient_error(Exception("429 quota exceeded")) is False

    def test_auth_not_transient(self) -> None:
        assert pf.is_transient_error(Exception("401 Unauthorized")) is False

    def test_value_error_not_transient(self) -> None:
        assert pf.is_transient_error(ValueError("bad input")) is False

    def test_empty_not_transient(self) -> None:
        assert pf.is_transient_error(Exception("")) is False


# ---------------------------------------------------------------------------
# is_provider_incompatible_error
# ---------------------------------------------------------------------------


class TestIsProviderIncompatibleError:
    def test_cohere_tool_plan_rejected(self) -> None:
        msg = (
            "invalid request: invalid message provided at index 3: "
            "`tool plan` cannot be used with this model."
        )
        assert pf.is_provider_incompatible_error(Exception(msg)) is True

    def test_tool_plan_marker_case_insensitive(self) -> None:
        assert (
            pf.is_provider_incompatible_error(Exception("TOOL_PLAN rejected")) is True
        )

    def test_quota_not_incompatible(self) -> None:
        assert (
            pf.is_provider_incompatible_error(Exception("429 quota exceeded")) is False
        )

    def test_transient_not_incompatible(self) -> None:
        assert (
            pf.is_provider_incompatible_error(Exception("connection refused")) is False
        )

    def test_unrelated_value_error(self) -> None:
        assert pf.is_provider_incompatible_error(ValueError("bad input")) is False

    def test_empty(self) -> None:
        assert pf.is_provider_incompatible_error(Exception("")) is False


# ---------------------------------------------------------------------------
# _provider_has_key
# ---------------------------------------------------------------------------


class TestProviderHasKey:
    def test_ollama_always_true(self) -> None:
        assert pf._provider_has_key("ollama") is True

    def test_openrouter_true_when_key_configured(self) -> None:
        from backend.settings import settings

        with patch.object(settings, "openrouter_api_key", "sk-or-v1-abc"):
            assert pf._provider_has_key("openrouter") is True

    def test_openrouter_false_when_key_absent(self) -> None:
        from backend.settings import settings

        with patch.object(settings, "openrouter_api_key", None):
            assert pf._provider_has_key("openrouter") is False

    def test_unknown_provider_false(self) -> None:
        assert pf._provider_has_key("does-not-exist") is False

    def test_google_genai_true_when_key_configured(self) -> None:
        """Regressão: o keymap usava 'google-genai' (hífen) mas o provider
        chega normalizado com underscore (mesma convenção de _provider_of) —
        o Gemini nunca era reconhecido como tendo key, então nunca entrava
        na cadeia de fallback de outro provider."""
        from backend.settings import settings

        with patch.object(settings, "google_api_key", "AIza-fake"):
            assert pf._provider_has_key("google_genai") is True

    def test_google_genai_false_when_key_absent(self) -> None:
        from backend.settings import settings

        with patch.object(settings, "google_api_key", None):
            assert pf._provider_has_key("google_genai") is False

    def test_llamacpp_is_available_with_external_url(self) -> None:
        from backend.settings import settings

        with patch.object(settings, "llamacpp_base_url", "http://127.0.0.1:18082/v1"):
            assert pf._provider_has_key("llamacpp") is True

    def test_llamacpp_is_available_for_managed_sidecar(self) -> None:
        from backend.settings import settings

        with (
            patch.object(settings, "llamacpp_base_url", None),
            patch(
                "backend.services.llamacpp_sidecar.llamacpp_status",
                return_value={"running": True},
            ),
        ):
            assert pf._provider_has_key("llamacpp") is True


# ---------------------------------------------------------------------------
# get_fallback_chain — fallback global removido
# ---------------------------------------------------------------------------


class TestGetFallbackChain:
    def test_global_chat_fallback_is_disabled_even_with_legacy_setting(self) -> None:
        from backend.workspace.runtime_settings import runtime_settings

        with patch.object(
            runtime_settings,
            "get",
            side_effect=lambda key, default=None: (
                ["google-genai:gemini-2.5-flash"]
                if key == "llm_fallback_order"
                else default
            ),
        ):
            assert pf.get_fallback_chain("openai:gpt-4o") == []


# ---------------------------------------------------------------------------
# record_switch / drain_switches
# ---------------------------------------------------------------------------


class TestSwitchQueue:
    def test_drain_empty(self) -> None:
        pf.drain_switches()  # limpa estado anterior
        assert pf.drain_switches() == []

    def test_record_then_drain(self) -> None:
        pf.drain_switches()
        pf.record_switch("openai:gpt-4o", "google-genai:gemini-2.5-flash")
        drained = pf.drain_switches()
        assert drained == [
            {"from": "openai:gpt-4o", "to": "google-genai:gemini-2.5-flash"}
        ]

    def test_drain_clears(self) -> None:
        pf.drain_switches()
        pf.record_switch("a:1", "b:2")
        pf.drain_switches()
        assert pf.drain_switches() == []

    def test_multiple_switches_in_order(self) -> None:
        pf.drain_switches()
        pf.record_switch("a:1", "b:2")
        pf.record_switch("b:2", "c:3")
        drained = pf.drain_switches()
        assert [d["from"] for d in drained] == ["a:1", "b:2"]
        assert [d["to"] for d in drained] == ["b:2", "c:3"]


# ---------------------------------------------------------------------------
# try_with_fallback
# ---------------------------------------------------------------------------


class TestTryWithFallback:
    async def test_success_first_no_switch(self) -> None:
        pf.drain_switches()
        calls = []

        async def fn(mid: str) -> str:
            calls.append(mid)
            return f"ok:{mid}"

        out = await pf.try_with_fallback(fn, "openai:gpt-4o")
        assert out == "ok:openai:gpt-4o"
        assert calls == ["openai:gpt-4o"]
        assert pf.drain_switches() == []

    async def test_quota_error_does_not_switch_model(self) -> None:
        pf.drain_switches()
        calls = []

        async def fn(mid: str) -> NoReturn:
            calls.append(mid)
            raise Exception("429 RESOURCE_EXHAUSTED")

        with patch.object(
            pf, "get_fallback_chain", return_value=["google-genai:gemini-2.5-flash"]
        ):
            with pytest.raises(pf.QuotaExhaustedError) as error:
                await pf.try_with_fallback(fn, "openai:gpt-4o")
        assert calls == ["openai:gpt-4o"]
        assert error.value.provider == "openai"
        assert pf.drain_switches() == []

    async def test_quota_error_preserves_selected_model(self) -> None:
        async def fn(mid: str) -> NoReturn:
            raise Exception("quota exceeded")

        with patch.object(
            pf,
            "get_fallback_chain",
            return_value=["google-genai:gemini-2.5-flash", "cohere:command-a"],
        ):
            with pytest.raises(pf.QuotaExhaustedError) as error:
                await pf.try_with_fallback(fn, "openai:gpt-4o")
        assert error.value.model_id == "openai:gpt-4o"

    async def test_exhausts_raises_quota(self) -> None:
        async def fn(mid: str) -> NoReturn:
            raise Exception("429 quota")

        with pytest.raises(pf.QuotaExhaustedError):
            await pf.try_with_fallback(fn, "openai:gpt-4o")

    async def test_empty_chain_raises_quota(self) -> None:
        async def fn(mid: str) -> NoReturn:
            raise Exception("rate limit")

        with patch.object(pf, "get_fallback_chain", return_value=[]):
            with pytest.raises(pf.QuotaExhaustedError):
                await pf.try_with_fallback(fn, "openai:gpt-4o")

    async def test_non_quota_propagates_immediately(self) -> None:
        calls = []

        async def fn(mid: str) -> NoReturn:
            calls.append(mid)
            raise ValueError("boom")

        with patch.object(
            pf, "get_fallback_chain", return_value=["google-genai:gemini-2.5-flash"]
        ):
            with pytest.raises(ValueError):
                await pf.try_with_fallback(fn, "openai:gpt-4o")
        assert calls == ["openai:gpt-4o"]  # não tentou fallback
