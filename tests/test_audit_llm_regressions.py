import base64
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.config.api_ada_configs import APIProvider, ModelInfo, TaskConfig
from src.llm_models.exceptions import NetworkConnectionError, ReqAbortException, RespParseException
from src.llm_models.model_client import openai_client, gemini_client
from src.llm_models.utils_model import LLMRequest, RequestType


def delta(index, name="search", arguments="{}"):
    return SimpleNamespace(index=index, id=f"call-{index}", function=SimpleNamespace(name=name, arguments=arguments))


class StreamDeltaTests(unittest.TestCase):
    def test_sparse_index_reports_protocol_error(self):
        chunk = SimpleNamespace(content=None, tool_calls=[delta(2)])
        with self.assertRaises(RespParseException):
            openai_client._process_delta(chunk, False, False, io.StringIO(), io.StringIO(), [])

    def test_all_tool_calls_in_one_chunk_are_preserved(self):
        chunks = SimpleNamespace(content=None, tool_calls=[delta(0), delta(1)])
        tools = []
        openai_client._process_delta(chunks, False, False, io.StringIO(), io.StringIO(), tools)
        self.assertEqual([item[0] for item in tools], ["call-0", "call-1"])

    def test_error_diagnostics_do_not_include_upstream_payload(self):
        try:
            try:
                raise ValueError("private upstream payload")
            except ValueError as cause:
                raise NetworkConnectionError() from cause
        except NetworkConnectionError as error:
            diagnostic = LLMRequest._get_original_error_info(error)
        self.assertIn("ValueError", diagnostic)
        self.assertNotIn("private upstream payload", diagnostic)


class ClientBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_stream_handler_network_failure_does_not_retry(self):
        request = object.__new__(LLMRequest)
        request.model_for_task = TaskConfig()
        model = ModelInfo("fixture", "fixture", "fixture", force_stream_mode=True)
        provider = APIProvider("fixture", "https://example.invalid", "fixture-key", retry_interval=0)
        client = SimpleNamespace(get_response=AsyncMock(side_effect=NetworkConnectionError()))
        with self.assertRaises(ReqAbortException):
            await request._attempt_request_on_model(
                model,
                provider,
                client,
                RequestType.RESPONSE,
                [],
                None,
                None,
                AsyncMock(),
                None,
                None,
                None,
                None,
                None,
            )
        self.assertEqual(client.get_response.await_count, 1)

    async def test_embedding_usage_requires_real_token_statistics(self):
        client = object.__new__(gemini_client.GeminiClient)
        model = ModelInfo("fixture", "fixture", "fixture")
        for token_count in (None, 3, 3.0):
            with self.subTest(token_count=token_count):
                response = SimpleNamespace(
                    embeddings=[SimpleNamespace(values=[1.0], statistics=SimpleNamespace(token_count=token_count))]
                )
                client.client = SimpleNamespace(
                    aio=SimpleNamespace(models=SimpleNamespace(embed_content=AsyncMock(return_value=response)))
                )
                result = await client.get_embedding(model, "many input characters")
                if token_count is None:
                    self.assertIsNone(result.usage)
                else:
                    self.assertEqual(result.usage.prompt_tokens, token_count)

    async def test_audio_payload_uses_detected_mime_type(self):
        client = object.__new__(gemini_client.GeminiClient)
        generate = AsyncMock()
        client.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
        model = ModelInfo("gemini-fixture", "fixture", "fixture")
        for data, mime in (
            (b"RIFF\x00\x00\x00\x00WAVE", "audio/wav"),
            (b"ID3fixture", "audio/mpeg"),
            (b"OggSfixture", "audio/ogg"),
        ):
            with (
                self.subTest(mime=mime),
                patch.object(gemini_client.prompt_manager, "format_prompt", return_value="transcribe"),
                patch.object(
                    gemini_client,
                    "_default_normal_response_parser",
                    return_value=(gemini_client.APIResponse(content="ok"), None),
                ),
            ):
                await client.get_audio_transcriptions(model, base64.b64encode(data).decode())
            audio = generate.await_args.kwargs["contents"][0].parts[1].inline_data
            self.assertEqual(audio.mime_type, mime)
            self.assertEqual(audio.data, data)
