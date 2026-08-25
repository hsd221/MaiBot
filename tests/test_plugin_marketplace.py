import json
import unittest

from pydantic import ValidationError

from src.plugin_system.marketplace import (
    ManifestV2,
    PluginRegistry,
    normalize_registry_url,
    normalize_repository_url,
    parse_manifest_v2,
    parse_plugin_registry,
    validate_market_plugin_id,
)


def manifest_v2(
    *,
    plugin_id: str = "github.alice.weather",
    version: str = "1.2.3",
    repository: str = "https://github.com/alice/riyabot-weather",
) -> dict:
    return {
        "manifest_version": 2,
        "id": plugin_id,
        "name": "天气插件",
        "version": version,
        "description": "查询天气信息",
        "author": {"name": "Alice", "url": "https://github.com/alice"},
        "license": "MIT",
        "urls": {
            "repository": repository,
            "homepage": f"{repository}#readme",
            "documentation": f"{repository}#readme",
            "issues": f"{repository}/issues",
        },
        "host_application": {"min_version": "0.14.0"},
        "sdk": {"min_version": "1.0.0", "max_version": "1.99.99"},
        "entrypoint": "plugin.py",
        "capabilities": ["network", "messages.send"],
        "i18n": {"default_locale": "zh-CN", "supported_locales": ["zh-CN"]},
        "keywords": ["weather"],
        "categories": ["Utility"],
    }


def registry_document(*, versions: list[dict] | None = None) -> dict:
    repository = "https://github.com/alice/riyabot-weather"
    return {
        "$meta": {
            "schema_version": 1,
            "name": "RiyaBot Official Plugin Market",
            "repository": "https://github.com/hsd221/RiyaBot-Plugins-Registry",
        },
        "plugins": {
            "github.alice.weather": {
                "repository": repository,
                "status": "approved",
                "review_level": "community",
                "updated_at": "2026-08-10T00:00:00Z",
                "versions": versions
                or [
                    {
                        "version": "1.2.3",
                        "ref": "v1.2.3",
                        "commit": "a" * 40,
                        "status": "approved",
                        "released_at": "2026-08-08T00:00:00Z",
                        "manifest": manifest_v2(),
                    }
                ],
            }
        },
    }


class ManifestV2Test(unittest.TestCase):
    def test_accepts_strict_v2_manifest_and_normalizes_repository_url(self) -> None:
        manifest = ManifestV2.model_validate(manifest_v2())

        self.assertEqual(manifest.id, "github.alice.weather")
        self.assertEqual(manifest.version, "1.2.3")
        self.assertEqual(str(manifest.urls.repository), "https://github.com/alice/riyabot-weather")

    def test_rejects_legacy_or_unsafe_v2_identity_and_entrypoint(self) -> None:
        invalid = manifest_v2(plugin_id="RiyaBot.riyabot")
        invalid["entrypoint"] = "../plugin.py"

        with self.assertRaises(ValidationError):
            ManifestV2.model_validate(invalid)

    def test_manifest_version_requires_a_real_integer(self) -> None:
        for value in (2.0, True, "2"):
            with self.subTest(value=repr(value)):
                document = manifest_v2()
                document["manifest_version"] = value

                with self.assertRaises(ValidationError):
                    ManifestV2.model_validate(document)

    def test_market_id_requires_lowercase_namespaced_segments(self) -> None:
        self.assertEqual(validate_market_plugin_id("github.alice.weather"), "github.alice.weather")

        for invalid in ("Alice.Weather", "alice.weather", "github.alice.weather_plugin", "github..weather"):
            with self.subTest(plugin_id=invalid), self.assertRaises(ValueError):
                validate_market_plugin_id(invalid)

    def test_parser_rejects_duplicate_manifest_fields(self) -> None:
        raw = json.dumps(manifest_v2())
        raw = raw.replace('"manifest_version": 2', '"manifest_version": 2, "manifest_version": 2', 1)

        with self.assertRaises(ValueError):
            parse_manifest_v2(raw)

    def test_urls_reject_invalid_hosts_and_preserve_ipv6_literals(self) -> None:
        invalid = manifest_v2(repository="https://bad host/alice/weather")
        with self.assertRaises(ValidationError):
            ManifestV2.model_validate(invalid)

        ipv6 = manifest_v2(repository="https://[2001:4860:4860::8888]/alice/weather")
        manifest = ManifestV2.model_validate(ipv6)
        self.assertEqual(
            manifest.urls.repository,
            "https://[2001:4860:4860::8888]/alice/weather",
        )

    def test_registry_url_rejects_query_parameters_and_fragments(self) -> None:
        for registry_url in (
            "https://plugins.riyabot.example/registry.json?token=secret",
            "https://plugins.riyabot.example/registry.json#snapshot",
        ):
            with self.subTest(registry_url=registry_url), self.assertRaises(ValueError):
                normalize_registry_url(registry_url)

    def test_urls_reject_raw_control_bidirectional_and_line_separator_characters(self) -> None:
        unsafe_urls = (
            "https://plugins.riyabot.example/registry.json\nheader: x",
            "https://plugins.riyabot.example/registry.json\rheader: x",
            "https://plugins.riyabot.example/registry.json\tvalue",
            "https://plugins.riyabot.example/registry.json\x00value",
            "https://plugins.riyabot.example/registry.json\u202e",
            "https://plugins.riyabot.example/registry.json\u2028",
            "https://plugins.riyabot.example/registry.json\u2029",
        )

        for unsafe_url in unsafe_urls:
            with self.subTest(unsafe_url=repr(unsafe_url)):
                with self.assertRaises(ValueError):
                    normalize_registry_url(unsafe_url)
                with self.assertRaises(ValueError):
                    normalize_repository_url(unsafe_url)

    def test_rejects_control_and_bidirectional_characters_in_display_text(self) -> None:
        cases: list[tuple[str, dict]] = []

        unsafe_name = manifest_v2()
        unsafe_name["name"] = "天气\x1b[31m插件"
        cases.append(("name", unsafe_name))

        unsafe_author = manifest_v2()
        unsafe_author["author"]["name"] = "Alice\u202e"
        cases.append(("author", unsafe_author))

        unsafe_license = manifest_v2()
        unsafe_license["license"] = "MIT\nApache-2.0"
        cases.append(("license", unsafe_license))

        unsafe_description = manifest_v2()
        unsafe_description["description"] = "查询天气\u2066伪装文本\u2069"
        cases.append(("description", unsafe_description))

        unsafe_keyword = manifest_v2()
        unsafe_keyword["keywords"] = ["weather\u200f"]
        cases.append(("keyword", unsafe_keyword))

        unsafe_category = manifest_v2()
        unsafe_category["categories"] = ["Utility\x85"]
        cases.append(("category", unsafe_category))

        unsafe_plugin_type = manifest_v2()
        unsafe_plugin_type["plugin_info"] = {"plugin_type": "general\ttool"}
        cases.append(("plugin_type", unsafe_plugin_type))

        unsafe_component = manifest_v2()
        unsafe_component["plugin_info"] = {
            "plugin_type": "general",
            "components": [
                {
                    "type": "tool",
                    "name": "天气\u202d工具",
                    "description": "查询\x00天气",
                }
            ],
        }
        cases.append(("component", unsafe_component))

        for field, document in cases:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ManifestV2.model_validate(document)

    def test_multiline_descriptions_preserve_chinese_and_joined_emoji(self) -> None:
        document = manifest_v2()
        document["description"] = "查询天气\n支持家庭出行 👨‍👩‍👧‍👦"
        document["plugin_info"] = {
            "components": [
                {
                    "type": "tool",
                    "name": "天气工具",
                    "description": "查询当前天气\n支持逐小时预报 ☀️",
                }
            ]
        }

        manifest = ManifestV2.model_validate(document)

        self.assertEqual(manifest.description, document["description"])
        self.assertEqual(
            manifest.plugin_info.components[0].description, document["plugin_info"]["components"][0]["description"]
        )

    def test_rejects_duplicate_keywords_and_categories(self) -> None:
        for field in ("keywords", "categories"):
            with self.subTest(field=field):
                document = manifest_v2()
                document[field] = ["duplicate", "duplicate"]

                with self.assertRaises(ValidationError):
                    ManifestV2.model_validate(document)

    def test_rejects_registry_ci_type_and_length_violations(self) -> None:
        cases: list[tuple[str, dict]] = []

        numeric_boolean = manifest_v2()
        numeric_boolean["plugin_info"] = {"is_built_in": 1}
        cases.append(("is_built_in", numeric_boolean))

        long_capability = manifest_v2()
        long_capability["capabilities"] = ["a" * 129]
        cases.append(("capability", long_capability))

        long_locale = manifest_v2()
        long_locale["i18n"] = {
            "default_locale": "en",
            "supported_locales": ["en", "en-" + "-".join(["abcdefgh"] * 5)],
        }
        cases.append(("supported_locale", long_locale))

        for field, document in cases:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ManifestV2.model_validate(document)


class PluginRegistryTest(unittest.TestCase):
    def test_registry_schema_version_rejects_boolean_alias_for_one(self) -> None:
        document = registry_document()
        document["$meta"]["schema_version"] = True

        with self.assertRaises(ValidationError):
            PluginRegistry.model_validate(document)

    def test_registry_schema_version_requires_a_real_integer(self) -> None:
        for value in (1.0, True, "1"):
            with self.subTest(value=repr(value)):
                document = registry_document()
                document["$meta"]["schema_version"] = value

                with self.assertRaises(ValidationError):
                    PluginRegistry.model_validate(document)

    def test_parses_top_level_plugin_entries_from_registry_contract(self) -> None:
        nested_document = registry_document()
        top_level_document = {
            "$meta": nested_document["$meta"],
            **nested_document["plugins"],
        }

        registry = PluginRegistry.model_validate(top_level_document)

        self.assertIn("github.alice.weather", registry.plugins)

    def test_parses_version_history_and_selects_latest_approved_stable_version(self) -> None:
        versions = [
            {
                "version": "1.2.3",
                "ref": "v1.2.3",
                "commit": "a" * 40,
                "status": "approved",
                "released_at": "2026-08-08T00:00:00Z",
                "manifest": manifest_v2(version="1.2.3"),
            },
            {
                "version": "1.3.0-beta.1",
                "ref": "v1.3.0-beta.1",
                "commit": "b" * 40,
                "status": "approved",
                "released_at": "2026-08-09T00:00:00Z",
                "manifest": manifest_v2(version="1.3.0-beta.1"),
            },
            {
                "version": "1.2.4",
                "ref": "v1.2.4",
                "commit": "c" * 40,
                "status": "yanked",
                "released_at": "2026-08-09T00:00:00Z",
                "manifest": manifest_v2(version="1.2.4"),
            },
        ]

        registry = PluginRegistry.model_validate(registry_document(versions=versions))

        latest = registry.latest_installable_version("github.alice.weather")
        self.assertEqual(latest.version, "1.2.3")

    def test_rejects_reusing_one_reviewed_ref_for_multiple_versions(self) -> None:
        versions = [
            registry_document()["plugins"]["github.alice.weather"]["versions"][0],
            {
                "version": "1.3.0",
                "ref": "v1.2.3",
                "commit": "b" * 40,
                "status": "approved",
                "released_at": "2026-08-09T00:00:00Z",
                "manifest": manifest_v2(version="1.3.0"),
            },
        ]

        with self.assertRaises(ValidationError):
            PluginRegistry.model_validate(registry_document(versions=versions))

    def test_rejects_reusing_one_reviewed_commit_for_multiple_versions(self) -> None:
        versions = [
            registry_document()["plugins"]["github.alice.weather"]["versions"][0],
            {
                "version": "1.3.0",
                "ref": "v1.3.0",
                "commit": "a" * 40,
                "status": "approved",
                "released_at": "2026-08-09T00:00:00Z",
                "manifest": manifest_v2(version="1.3.0"),
            },
        ]

        with self.assertRaises(ValidationError):
            PluginRegistry.model_validate(registry_document(versions=versions))

    def test_same_precedence_versions_use_release_time_then_identity_as_tiebreakers(self) -> None:
        older_build = {
            "version": "1.2.3+build.2",
            "ref": "v1.2.3-build.2",
            "commit": "b" * 40,
            "status": "approved",
            "released_at": "2026-08-08T00:00:00Z",
            "manifest": manifest_v2(version="1.2.3+build.2"),
        }
        newer_build = {
            "version": "1.2.3+build.1",
            "ref": "v1.2.3-build.1",
            "commit": "a" * 40,
            "status": "approved",
            "released_at": "2026-08-09T00:00:00Z",
            "manifest": manifest_v2(version="1.2.3+build.1"),
        }

        registry = PluginRegistry.model_validate(registry_document(versions=[older_build, newer_build]))
        self.assertEqual(
            registry.latest_installable_version("github.alice.weather").version,
            "1.2.3+build.1",
        )

        newer_build["released_at"] = older_build["released_at"]
        registry = PluginRegistry.model_validate(registry_document(versions=[newer_build, older_build]))
        self.assertEqual(
            registry.latest_installable_version("github.alice.weather").version,
            "1.2.3+build.2",
        )

    def test_rejects_registry_manifest_identity_version_or_repository_mismatch(self) -> None:
        cases = []

        wrong_id = registry_document()
        wrong_id["plugins"]["github.alice.weather"]["versions"][0]["manifest"]["id"] = "github.alice.other"
        cases.append(wrong_id)

        wrong_version = registry_document()
        wrong_version["plugins"]["github.alice.weather"]["versions"][0]["manifest"]["version"] = "9.9.9"
        cases.append(wrong_version)

        wrong_repository = registry_document()
        wrong_repository["plugins"]["github.alice.weather"]["versions"][0]["manifest"]["urls"]["repository"] = (
            "https://github.com/mallory/weather"
        )
        cases.append(wrong_repository)

        for document in cases:
            with self.subTest(document=document), self.assertRaises(ValidationError):
                PluginRegistry.model_validate(document)

    def test_rejects_control_characters_in_registry_display_metadata(self) -> None:
        document = registry_document()
        document["$meta"]["name"] = "RiyaBot Market\u202e"

        with self.assertRaises(ValidationError):
            PluginRegistry.model_validate(document)

    def test_rejects_duplicate_json_keys_before_schema_validation(self) -> None:
        raw = json.dumps(registry_document())
        raw = raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1)

        with self.assertRaises(ValueError):
            parse_plugin_registry(raw)

    def test_artifact_url_and_sha256_must_be_declared_together(self) -> None:
        document = registry_document()
        document["plugins"]["github.alice.weather"]["versions"][0]["artifact_url"] = (
            "https://github.com/alice/riyabot-weather/releases/download/v1.2.3/plugin.zip"
        )

        with self.assertRaises(ValidationError):
            PluginRegistry.model_validate(document)

    def test_registry_timestamps_must_include_a_timezone(self) -> None:
        cases = []

        naive_plugin_timestamp = registry_document()
        naive_plugin_timestamp["plugins"]["github.alice.weather"]["updated_at"] = "2026-08-08T00:00:00"
        cases.append(naive_plugin_timestamp)

        naive_release_timestamp = registry_document()
        naive_release_timestamp["plugins"]["github.alice.weather"]["versions"][0]["released_at"] = "2026-08-08T00:00:00"
        cases.append(naive_release_timestamp)

        for document in cases:
            with self.subTest(document=document), self.assertRaises(ValidationError):
                PluginRegistry.model_validate(document)

    def test_registry_update_time_cannot_precede_latest_release(self) -> None:
        document = registry_document()
        document["plugins"]["github.alice.weather"]["updated_at"] = "2026-08-07T23:59:59Z"

        with self.assertRaises(ValidationError):
            PluginRegistry.model_validate(document)

    def test_registry_timestamps_require_iso_text_instead_of_numeric_coercion(self) -> None:
        cases = []

        numeric_update = registry_document()
        numeric_update["plugins"]["github.alice.weather"]["updated_at"] = 1_787_486_400
        cases.append(numeric_update)

        numeric_text_update = registry_document()
        numeric_text_update["plugins"]["github.alice.weather"]["updated_at"] = "1787486400"
        cases.append(numeric_text_update)

        numeric_release = registry_document()
        numeric_release["plugins"]["github.alice.weather"]["versions"][0]["released_at"] = 1_787_486_400
        numeric_release["plugins"]["github.alice.weather"]["updated_at"] = "2030-01-01T00:00:00Z"
        cases.append(numeric_release)

        for document in cases:
            with self.subTest(document=document), self.assertRaises(ValidationError):
                PluginRegistry.model_validate(document)

    def test_registry_requires_explicit_review_status_and_level(self) -> None:
        paths = (
            ("plugin_status", ("plugins", "github.alice.weather"), "status"),
            ("review_level", ("plugins", "github.alice.weather"), "review_level"),
            (
                "version_status",
                ("plugins", "github.alice.weather", "versions", 0),
                "status",
            ),
        )

        for label, path, field in paths:
            with self.subTest(field=label):
                document = registry_document()
                target = document
                for part in path:
                    target = target[part]
                del target[field]

                with self.assertRaises(ValidationError):
                    PluginRegistry.model_validate(document)


if __name__ == "__main__":
    unittest.main()
