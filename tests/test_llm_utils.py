import asyncio
import base64
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from src.config.api_ada_configs import APIProvider, ModelInfo, TaskConfig
from src.llm_models import exceptions
from src.llm_models.model_client.base_client import APIResponse, UsageRecord
from src.llm_models.payload_content.message import MessageBuilder, RoleType
from src.llm_models.payload_content.tool_option import ToolParamType
from src.llm_models.utils import LLMUsageRecorder, compress_messages
from src.llm_models import utils as llm_utils
from src.llm_models.exceptions import EmptyResponseException, ModelAttemptFailed, ReqAbortException, RespNotOkException
from src.llm_models.utils_model import LLMRequest, RequestType


def _png_base64() -> str:
    buffer = io.BytesIO()
    Image.new("RGBA", (16, 16), (255, 0, 0, 128)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _large_png_base64() -> str:
    image = Image.new("RGBA", (256, 256))
    value = 1
    pixels = []
    for _ in range(image.width * image.height):
        value = (1103515245 * value + 12345) & 0x7FFFFFFF
        pixels.append((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, 255))
    image.putdata(pixels)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class LLMExceptionsTest(unittest.TestCase):
    def test_exceptions_format_defaults_mapped_codes_custom_messages_and_original_errors(self) -> None:
        self.assertEqual(str(exceptions.NetworkConnectionError()), "连接异常，请检查网络连接状态或URL是否正确")
        self.assertEqual(str(exceptions.ReqAbortException()), "请求因未知原因异常终止")
        self.assertEqual(str(exceptions.RespNotOkException(401)), exceptions.error_code_mapping[401])
        self.assertEqual(str(exceptions.RespNotOkException(418, "teapot")), "teapot")
        self.assertEqual(str(exceptions.RespNotOkException(499)), "未知的异常响应代码：499")
        self.assertEqual(
            str(exceptions.RespParseException({"raw": "bad"})),
            "解析响应内容时发生未知错误，请检查是否配置了正确的解析方法",
        )
        self.assertEqual(str(exceptions.EmptyResponseException()), "响应内容为空，这可能是一个临时性问题")

        original = RuntimeError("network down")
        failed = exceptions.ModelAttemptFailed("failed", original_exception=original)

        self.assertEqual(str(failed), "failed")
        self.assertIs(failed.original_exception, original)


class LLMCompressMessagesTest(unittest.TestCase):
    def test_compress_messages_reformats_static_images_and_keeps_text_order(self) -> None:
        image_base64 = _png_base64()
        message = MessageBuilder().add_text_content("look").add_image_content("png", image_base64).build()

        compressed = compress_messages([message], img_target_size=10_000_000)[0]

        self.assertEqual(compressed.content[0], "look")
        self.assertEqual(compressed.content[1][0], "png")
        self.assertTrue(base64.b64decode(compressed.content[1][1]).startswith(b"\x89PNG"))

    def test_compress_messages_leaves_non_multimodal_messages_unchanged(self) -> None:
        text_message = MessageBuilder().add_text_content("hello").build()

        compressed = compress_messages([text_message])

        self.assertIs(compressed[0], text_message)

    def test_compress_messages_applies_aggregate_budget_without_changing_png_format(self) -> None:
        image_base64 = _large_png_base64()
        message = (
            MessageBuilder()
            .add_image_content("png", image_base64)
            .add_text_content("next")
            .add_image_content("png", image_base64)
            .build()
        )

        compressed = compress_messages(
            [message],
            img_target_size=10_000_000,
            img_total_target_size=20_000,
        )[0]

        compressed_images = [item for item in compressed.content if isinstance(item, tuple)]
        self.assertEqual([image_format for image_format, _ in compressed_images], ["png", "png"])
        self.assertTrue(all(len(data) < len(image_base64) for _, data in compressed_images))
        self.assertEqual(compressed.content[1], "next")


class LLMUsageRecorderTest(unittest.TestCase):
    def test_record_usage_calculates_costs_and_persists_rounded_fields(self) -> None:
        recorder = LLMUsageRecorder.__new__(LLMUsageRecorder)
        model = ModelInfo(
            model_identifier="provider-model",
            name="model-a",
            api_provider="provider-a",
            price_in=2.0,
            price_out=4.0,
        )
        usage = UsageRecord(
            model_name="model-a",
            provider_name="provider-a",
            prompt_tokens=500_000,
            completion_tokens=250_000,
            total_tokens=750_000,
        )

        with patch.object(llm_utils.LLMUsage, "create") as create:
            recorder.record_usage_to_database(
                model_info=model,
                model_usage=usage,
                user_id="user-1",
                request_type="reply",
                endpoint="/chat/completions",
                time_cost=1.23456,
            )

        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["model_name"], "provider-model")
        self.assertEqual(kwargs["model_assign_name"], "model-a")
        self.assertEqual(kwargs["prompt_tokens"], 500_000)
        self.assertEqual(kwargs["completion_tokens"], 250_000)
        self.assertEqual(kwargs["total_tokens"], 750_000)
        self.assertEqual(kwargs["cost"], 2.0)
        self.assertEqual(kwargs["time_cost"], 1.235)
        self.assertEqual(kwargs["status"], "success")


class CapturingClient:
    def __init__(self, response: APIResponse | None = None):
        self.calls = []
        self.response = response or APIResponse(content="ok")

    async def get_response(self, **kwargs):
        self.calls.append(("response", kwargs))
        return self.response

    async def get_embedding(self, **kwargs):
        self.calls.append(("embedding", kwargs))
        return self.response

    async def get_audio_transcriptions(self, **kwargs):
        self.calls.append(("audio", kwargs))
        return self.response


class RequestTooLargeThenSuccessClient(CapturingClient):
    async def get_response(self, **kwargs):
        self.calls.append(("response", kwargs))
        if len(self.calls) == 1:
            raise RespNotOkException(413)
        return self.response


class LLMRequestHelpersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.task = TaskConfig(
            model_list=["model-a", "model-b"],
            max_tokens=64,
            temperature=0.2,
            selection_strategy="balance",
        )

    def test_build_tool_options_keeps_valid_tools_and_skips_invalid_parameter_definitions(self) -> None:
        request = LLMRequest(self.task, request_type="reply")

        options = request._build_tool_options(
            [
                {
                    "name": "search",
                    "description": "Search docs",
                    "parameters": [("query", ToolParamType.STRING, "Query", True, ["docs", "web"])],
                },
                {
                    "name": "broken",
                    "description": "Broken tool",
                    "parameters": [("count", "integer", "Count", False, None)],
                },
            ]
        )

        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].name, "search")
        self.assertEqual(options[0].params[0].enum_values, ["docs", "web"])

    def test_extract_reasoning_removes_first_think_block_and_reports_original_exception_info(self) -> None:
        content, reasoning = LLMRequest._extract_reasoning("<think>hidden</think>\nanswer</think>")

        self.assertEqual(content, "answer</think>")
        self.assertEqual(reasoning, "hidden")

        try:
            try:
                raise KeyError("root")
            except KeyError as exc:
                raise RuntimeError("wrapped") from exc
        except RuntimeError as exc:
            original_info = LLMRequest._get_original_error_info(exc)

        self.assertIn("底层异常类型: KeyError", original_info)
        self.assertNotIn("root", original_info)

    async def test_generate_response_for_images_labels_and_sends_frames_in_order(self) -> None:
        request = LLMRequest(self.task, request_type="image")
        captured_messages = []

        async def fake_execute_request(**kwargs):
            client = SimpleNamespace(get_support_image_formats=lambda: ["png"])
            captured_messages.extend(kwargs["message_factory"](client))
            return APIResponse(content="逐帧与整体概括"), ModelInfo(
                model_identifier="a",
                name="model-a",
                api_provider="provider-a",
            )

        with (
            patch.object(request, "_execute_request", side_effect=fake_execute_request),
            patch("src.llm_models.utils_model.llm_usage_recorder.record_usage_to_database"),
        ):
            content, _ = await request.generate_response_for_images(
                "请分析动图",
                [("png", "frame-1"), ("png", "frame-2")],
                temperature=0.4,
                start_index=17,
            )

        self.assertEqual(content, "逐帧与整体概括")
        self.assertEqual(
            captured_messages[0].content,
            ["请分析动图", "第 17 帧：", ("png", "frame-1"), "第 18 帧：", ("png", "frame-2")],
        )

    async def test_generate_response_async_sends_system_and_user_as_distinct_messages(self) -> None:
        request = LLMRequest(self.task, request_type="reply")
        captured_messages = []

        async def fake_execute_request(**kwargs):
            captured_messages.extend(kwargs["message_factory"](SimpleNamespace()))
            return APIResponse(content="ok"), ModelInfo(
                model_identifier="a",
                name="model-a",
                api_provider="provider-a",
            )

        with (
            patch.object(request, "_execute_request", side_effect=fake_execute_request),
            patch("src.llm_models.utils_model.llm_usage_recorder.record_usage_to_database"),
        ):
            content, _ = await request.generate_response_async(
                "本轮动态输入",
                system_prompt="稳定系统约束",
            )

        self.assertEqual(content, "ok")
        self.assertEqual([message.role for message in captured_messages], [RoleType.System, RoleType.User])
        self.assertEqual([message.content for message in captured_messages], ["稳定系统约束", "本轮动态输入"])

    def test_select_model_uses_balance_scores_exclusions_and_embedding_force_new_clients(self) -> None:
        request = LLMRequest(self.task, request_type="embedding")
        request.model_usage = {"model-a": (200, 0, 0), "model-b": (10, 0, 0)}
        provider = APIProvider(name="provider-a", base_url="https://api.example.test", api_key="secret")
        models = {
            "model-a": ModelInfo(model_identifier="a", name="model-a", api_provider="provider-a"),
            "model-b": ModelInfo(model_identifier="b", name="model-b", api_provider="provider-a"),
        }
        fake_model_config = SimpleNamespace(
            get_model_info=lambda name: models[name],
            get_provider=lambda name: provider,
        )
        fake_client = object()

        with (
            patch("src.llm_models.utils_model.model_config", fake_model_config),
            patch(
                "src.llm_models.utils_model.client_registry.get_client_class_instance", return_value=fake_client
            ) as get_client,
        ):
            model_info, api_provider, client = request._select_model(exclude_models={"model-a"})

        self.assertEqual(model_info.name, "model-b")
        self.assertIs(api_provider, provider)
        self.assertIs(client, fake_client)
        get_client.assert_called_once_with(provider, force_new=True)
        self.assertEqual(request.model_usage["model-b"], (10, 0, 1))

    def test_select_model_uses_runtime_config_override_for_custom_embedding_request(self) -> None:
        task = TaskConfig(model_list=["model-a"], selection_strategy="balance")
        request = LLMRequest(task, request_type="emoji.vector.index", model_config_override=SimpleNamespace())
        provider = APIProvider(name="provider-runtime", base_url="https://api.example.test", api_key="secret")
        model = ModelInfo(
            model_identifier="runtime-id",
            name="model-a",
            api_provider="provider-runtime",
        )
        override = SimpleNamespace(
            get_model_info=lambda name: model,
            get_provider=lambda name: provider,
        )
        request.model_config_override = override

        with patch(
            "src.llm_models.utils_model.client_registry.get_client_class_instance", return_value=object()
        ) as get_client:
            selected_model, selected_provider, _ = request._select_model(request_type=RequestType.EMBEDDING)

        self.assertIs(selected_model, model)
        self.assertIs(selected_provider, provider)
        get_client.assert_called_once_with(provider, force_new=True)

    async def test_attempt_response_uses_request_then_model_then_task_generation_defaults(self) -> None:
        request = LLMRequest(self.task, request_type="reply")
        provider = APIProvider(
            name="provider-a",
            base_url="https://api.example.test",
            api_key="secret",
            max_retry=1,
            retry_interval=0,
        )
        model = ModelInfo(
            model_identifier="a",
            name="model-a",
            api_provider="provider-a",
            extra_params={"temperature": 0.7, "max_tokens": 33},
        )
        client = CapturingClient()
        message = MessageBuilder().add_text_content("hello").build()

        response = await request._attempt_request_on_model(
            model,
            provider,
            client,
            RequestType.RESPONSE,
            message_list=[message],
            tool_options=None,
            response_format=None,
            stream_response_handler=None,
            async_response_parser=None,
            temperature=None,
            max_tokens=None,
            embedding_input=None,
            audio_base64=None,
        )

        self.assertEqual(response.content, "ok")
        kwargs = client.calls[0][1]
        self.assertEqual(kwargs["temperature"], 0.7)
        self.assertEqual(kwargs["max_tokens"], 33)
        self.assertEqual(kwargs["extra_params"], {"temperature": 0.7, "max_tokens": 33})

    async def test_attempt_response_retries_413_after_compressing_messages_without_consuming_retry(self) -> None:
        request = LLMRequest(self.task, request_type="reply")
        provider = APIProvider(
            name="provider-a",
            base_url="https://api.example.test",
            api_key="secret",
            max_retry=1,
            retry_interval=0,
        )
        model = ModelInfo(model_identifier="a", name="model-a", api_provider="provider-a")
        client = RequestTooLargeThenSuccessClient()
        message = MessageBuilder().add_text_content("look").add_image_content("png", _png_base64()).build()

        response = await request._attempt_request_on_model(
            model,
            provider,
            client,
            RequestType.RESPONSE,
            message_list=[message],
            tool_options=None,
            response_format=None,
            stream_response_handler=None,
            async_response_parser=None,
            temperature=0.4,
            max_tokens=12,
            embedding_input=None,
            audio_base64=None,
        )

        self.assertEqual(response.content, "ok")
        self.assertEqual(len(client.calls), 2)
        first_call_messages = client.calls[0][1]["message_list"]
        second_call_messages = client.calls[1][1]["message_list"]
        self.assertIs(first_call_messages[0], message)
        self.assertIsNot(second_call_messages[0], message)
        self.assertEqual(second_call_messages[0].content[1][0], "png")
        self.assertTrue(base64.b64decode(second_call_messages[0].content[1][1]).startswith(b"\x89PNG"))

    async def test_attempt_request_wraps_exhausted_empty_responses_in_model_attempt_failed(self) -> None:
        class EmptyClient(CapturingClient):
            async def get_response(self, **kwargs):
                raise EmptyResponseException("empty")

        request = LLMRequest(self.task, request_type="reply")
        provider = APIProvider(
            name="provider-a",
            base_url="https://api.example.test",
            api_key="secret",
            max_retry=1,
            retry_interval=0,
        )
        model = ModelInfo(model_identifier="a", name="model-a", api_provider="provider-a")

        with self.assertRaises(ModelAttemptFailed) as raised:
            await request._attempt_request_on_model(
                model,
                provider,
                EmptyClient(),
                RequestType.RESPONSE,
                message_list=[MessageBuilder().add_text_content("hello").build()],
                tool_options=None,
                response_format=None,
                stream_response_handler=None,
                async_response_parser=None,
                temperature=None,
                max_tokens=None,
                embedding_input=None,
                audio_base64=None,
            )

        self.assertIsInstance(raised.exception.original_exception, EmptyResponseException)

    async def test_execute_request_records_effective_request_and_successful_response(self) -> None:
        task = TaskConfig(
            model_list=["model-a"],
            max_tokens=64,
            temperature=0.2,
            selection_strategy="balance",
        )
        request = LLMRequest(task, request_type="reply.main")
        provider = APIProvider(
            name="provider-a",
            base_url="https://api.example.test",
            api_key="secret",
            max_retry=1,
            retry_interval=0,
        )
        model = ModelInfo(
            model_identifier="provider-model",
            name="model-a",
            api_provider="provider-a",
            temperature=0.35,
            max_tokens=96,
        )
        response = APIResponse(
            content="ok",
            usage=UsageRecord("model-a", "provider-a", 8, 3, 11),
        )
        client = CapturingClient(response)

        with (
            patch.object(request, "_select_model", return_value=(model, provider, client)),
            patch("src.llm_models.utils_model.model_request_trace_recorder") as recorder,
        ):
            recorder.start_trace.return_value = 42
            actual, actual_model = await request._execute_request(
                request_type=RequestType.RESPONSE,
                message_factory=lambda _client: [
                    MessageBuilder().add_text_content("hello").add_image_content("png", "aGVsbG8=").build()
                ],
            )

        self.assertIs(actual, response)
        self.assertIs(actual_model, model)
        trace_kwargs = recorder.start_trace.call_args.kwargs
        self.assertEqual(trace_kwargs["request_type"], "reply.main")
        self.assertEqual(trace_kwargs["operation"], "response")
        self.assertEqual(trace_kwargs["request_payload"]["parameters"]["temperature"], 0.35)
        self.assertEqual(trace_kwargs["request_payload"]["parameters"]["max_tokens"], 96)
        self.assertEqual(trace_kwargs["request_payload"]["messages"][0]["content"][0]["text"], "hello")
        self.assertEqual(trace_kwargs["request_payload"]["messages"][0]["content"][1]["media_id"], "image-1")
        self.assertEqual(trace_kwargs["request_media"][0].media_id, "image-1")
        self.assertEqual(trace_kwargs["request_media"][0].base64_data, "aGVsbG8=")
        recorder.finish_success.assert_called_once()
        self.assertEqual(recorder.finish_success.call_args.args[:2], (42, response))

    async def test_execute_request_finishes_error_trace_without_changing_failure_semantics(self) -> None:
        class HardFailClient(CapturingClient):
            async def get_response(self, **kwargs):
                self.calls.append(("response", kwargs))
                raise RespNotOkException(400)

        task = TaskConfig(
            model_list=["model-a"],
            max_tokens=64,
            temperature=0.2,
            selection_strategy="balance",
        )
        request = LLMRequest(task, request_type="planner.main")
        provider = APIProvider(
            name="provider-a",
            base_url="https://api.example.test",
            api_key="secret",
            max_retry=1,
            retry_interval=0,
        )
        model = ModelInfo(model_identifier="provider-model", name="model-a", api_provider="provider-a")

        with (
            patch.object(request, "_select_model", return_value=(model, provider, HardFailClient())),
            patch("src.llm_models.utils_model.model_request_trace_recorder") as recorder,
        ):
            recorder.start_trace.return_value = 73
            with self.assertRaises(RespNotOkException):
                await request._execute_request(
                    request_type=RequestType.RESPONSE,
                    message_factory=lambda _client: [MessageBuilder().add_text_content("hello").build()],
                )

        recorder.finish_error.assert_called_once()
        self.assertEqual(recorder.finish_error.call_args.args[0], 73)
        self.assertIsInstance(recorder.finish_error.call_args.args[1], RespNotOkException)

    async def test_execute_request_finishes_trace_when_task_is_cancelled(self) -> None:
        class CancelledClient(CapturingClient):
            async def get_response(self, **kwargs):
                self.calls.append(("response", kwargs))
                raise asyncio.CancelledError

        task = TaskConfig(
            model_list=["model-a"],
            max_tokens=64,
            temperature=0.2,
            selection_strategy="balance",
        )
        request = LLMRequest(task, request_type="reply.main")
        provider = APIProvider(
            name="provider-a",
            base_url="https://api.example.test",
            api_key="secret",
            max_retry=1,
            retry_interval=0,
        )
        model = ModelInfo(model_identifier="provider-model", name="model-a", api_provider="provider-a")

        with (
            patch.object(request, "_select_model", return_value=(model, provider, CancelledClient())),
            patch("src.llm_models.utils_model.model_request_trace_recorder") as recorder,
        ):
            recorder.start_trace.return_value = 91
            with self.assertRaises(asyncio.CancelledError):
                await request._execute_request(
                    request_type=RequestType.RESPONSE,
                    message_factory=lambda _client: [MessageBuilder().add_text_content("hello").build()],
                )

        recorder.finish_error.assert_called_once()
        self.assertEqual(recorder.finish_error.call_args.args[0], 91)
        self.assertIsInstance(recorder.finish_error.call_args.args[1], ReqAbortException)

    async def test_generate_response_allows_empty_result_when_requested(self) -> None:
        class EmptyClient(CapturingClient):
            async def get_response(self, **kwargs):
                self.calls.append(("response", kwargs))
                raise EmptyResponseException("empty")

        task = TaskConfig(
            model_list=["model-a"],
            max_tokens=64,
            temperature=0.2,
            selection_strategy="balance",
        )
        request = LLMRequest(task, request_type="planner")
        provider = APIProvider(
            name="provider-a",
            base_url="https://api.example.test",
            api_key="secret",
            max_retry=2,
            retry_interval=0,
        )
        model = ModelInfo(model_identifier="a", name="model-a", api_provider="provider-a")
        client = EmptyClient()

        with patch.object(request, "_select_model", return_value=(model, provider, client)):
            content, (reasoning, model_name, tool_calls) = await request.generate_response_async(
                "stay silent",
                raise_when_empty=False,
            )

        self.assertEqual((content, reasoning, model_name, tool_calls), ("", "", "model-a", None))
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
