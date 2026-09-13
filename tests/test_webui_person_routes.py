import datetime
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from src.memory.user_profile import UserProfile
from src.webui import person_routes


class FakeProfileStore:
    def __init__(self, profiles: dict[str, UserProfile]) -> None:
        self.profiles = profiles
        self.saved: list[str] = []
        self.deleted: list[str] = []

    def list_profiles(self) -> list[str]:
        return list(self.profiles.keys())

    def iter_profiles(self):
        return iter(self.profiles.values())

    def get_profile(self, user_id: str) -> UserProfile | None:
        return self.profiles.get(user_id)

    def save_profile(self, profile: UserProfile) -> None:
        self.profiles[profile.user_id] = profile
        self.saved.append(profile.user_id)

    def profile_exists(self, user_id: str) -> bool:
        return user_id in self.profiles

    def delete_profile(self, user_id: str) -> None:
        self.profiles.pop(user_id, None)
        self.deleted.append(user_id)


def make_profile(
    user_id: str,
    *,
    platform: str = "qq",
    person_name: str | None = None,
    nickname: str | None = None,
    is_known: bool = True,
    impression: str = "",
    interests: list[str] | None = None,
) -> UserProfile:
    created_at = datetime.datetime(2026, 1, 2, 3, 4, 5)
    updated_at = datetime.datetime(2026, 1, 3, 3, 4, 5)
    meta = {
        "platform": platform,
        "is_known": is_known,
    }
    if person_name is not None:
        meta["person_name"] = person_name
    if nickname is not None:
        meta["nickname"] = nickname

    return UserProfile(
        user_id=user_id,
        traits={"friendly": 0.8},
        interests=interests or [],
        preferences={"food": "面"},
        facts={
            "platform": platform,
            "person_name": person_name or user_id,
            "nickname": nickname or "",
            "group_nick_name": '[{"group_id": "g1", "nickname": "群名"}]',
        },
        stats={
            person_routes._WEBUI_META_KEY: meta,
            "_private_counter": 9,
            "message_count": 3,
        },
        mood_history=[{"mood": "happy"}],
        impression=impression,
        expression_style="短句",
        expression_patterns={"punctuation": "少"},
        created_at=created_at,
        updated_at=updated_at,
        last_extracted_at=updated_at,
    )


class PersonRouteHelpersTest(unittest.TestCase):
    def test_verify_auth_token_delegates_to_shared_auth_checker(self) -> None:
        with patch.object(person_routes, "verify_auth_token_from_cookie_or_header", return_value=True) as verify:
            self.assertTrue(person_routes.verify_auth_token("cookie", "Bearer token"))

        verify.assert_called_once_with("cookie", "Bearer token")

    def test_profile_conversion_prefers_webui_meta_and_filters_internal_stats(self) -> None:
        profile = make_profile(
            "user-1",
            platform="qq",
            person_name="元数据名",
            nickname="元昵称",
            is_known=False,
            impression="记忆点",
            interests=["音乐"],
        )
        profile.stats[person_routes._WEBUI_META_KEY]["name_reason"] = "人工修正"
        profile.stats[person_routes._WEBUI_META_KEY]["memory_points"] = "手工记忆点"

        person = person_routes.profile_to_person_dict(profile)

        self.assertEqual(person["person_id"], "user-1")
        self.assertEqual(person["person_name"], "元数据名")
        self.assertEqual(person["nickname"], "元昵称")
        self.assertEqual(person["name_reason"], "人工修正")
        self.assertEqual(person["memory_points"], "手工记忆点")
        self.assertFalse(person["is_known"])
        self.assertEqual(person["platform"], "qq")
        self.assertEqual(person["group_nick_name"], [{"group_id": "g1", "nickname": "群名"}])
        self.assertEqual(person["profile_stats"], {"message_count": 3})
        self.assertEqual(person["mood_history_count"], 1)
        self.assertEqual(person["profile_expression_style"], "短句")
        self.assertIsInstance(person["know_since"], float)

    def test_profile_conversion_exposes_platform_identity_and_verification_fields(self) -> None:
        profile = UserProfile(
            user_id="42",
            platform="qq",
            nickname="QQ 昵称",
            cardname="群名片",
            group_nicknames=[
                {
                    "platform": "qq",
                    "group_id": "g1",
                    "group_name": "测试群",
                    "group_nick_name": "群名片",
                }
            ],
            person_type="person",
            identity_source="message_sender",
            verification_status="verified",
        )

        person = person_routes.profile_to_person_dict(profile)

        self.assertEqual(person["person_id"], "qq:42")
        self.assertEqual(person["id"], person_routes._stable_int_id("qq:42"))
        self.assertEqual(person["platform"], "qq")
        self.assertEqual(person["nickname"], "QQ 昵称")
        self.assertEqual(person["cardname"], "群名片")
        self.assertEqual(person["person_type"], "person")
        self.assertEqual(person["identity_source"], "message_sender")
        self.assertEqual(person["verification_status"], "verified")
        self.assertTrue(person["is_known"])

    def test_parse_and_timestamp_helpers_handle_invalid_values(self) -> None:
        self.assertEqual(person_routes.parse_group_nick_name('[{"group_id": "1"}]'), [{"group_id": "1"}])
        self.assertIsNone(person_routes.parse_group_nick_name("not json"))
        self.assertIsNone(person_routes.parse_group_nick_name(None))
        self.assertEqual(person_routes._stable_int_id("user-1"), person_routes._stable_int_id("user-1"))
        self.assertEqual(person_routes._to_timestamp("12.5"), 12.5)
        self.assertIsNone(person_routes._to_timestamp("bad"))
        self.assertIn("music", person_routes._profile_search_text({"profile_interests": ["music"]}))

    def test_list_and_stats_use_profile_store_filters_search_platform_known_and_limit(self) -> None:
        store = FakeProfileStore(
            {
                "u1": make_profile("u1", platform="qq", person_name="Alice", is_known=True, interests=["music"]),
                "u2": make_profile("u2", platform="discord", person_name="Bob", is_known=False, interests=["game"]),
                "u3": make_profile("u3", platform="qq", person_name="Carol", is_known=True, interests=["paint"]),
            }
        )

        with patch.object(person_routes, "ProfileStore", return_value=store):
            qq_people = person_routes.list_profile_person_dicts(platform="qq", is_known=True)
            searched = person_routes.list_profile_person_dicts(search="game")
            limited = person_routes.list_profile_person_dicts(limit=2)
            stats = person_routes.get_profile_person_stats()
            detail = person_routes.get_profile_person_dict("u1")

        self.assertEqual([person["person_id"] for person in qq_people], ["u1", "u3"])
        self.assertEqual([person["person_id"] for person in searched], ["u2"])
        self.assertEqual(len(limited), 2)
        self.assertEqual(stats, {"total": 3, "known": 2, "unknown": 1, "platforms": {"qq": 2, "discord": 1}})
        self.assertEqual(detail["person_name"], "Alice")


class PersonRouteEndpointTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = FakeProfileStore(
            {
                "u1": make_profile("u1", platform="qq", person_name="Alice", nickname="Ali", is_known=True),
                "u2": make_profile("u2", platform="discord", person_name="Bob", is_known=False),
            }
        )
        self.auth_patch = patch.object(person_routes, "verify_auth_token", return_value=True)
        self.store_patch = patch.object(person_routes, "ProfileStore", return_value=self.store)
        self.auth_patch.start()
        self.store_patch.start()

    def tearDown(self) -> None:
        self.store_patch.stop()
        self.auth_patch.stop()

    async def test_person_list_stats_and_detail_routes_return_profile_data(self) -> None:
        listed = await person_routes.get_person_list(
            page=1,
            page_size=1,
            search=None,
            is_known=None,
            platform=None,
        )
        stats = await person_routes.get_person_stats()
        detail = await person_routes.get_person_detail("u1")

        self.assertEqual(listed.total, 2)
        self.assertEqual(len(listed.data), 1)
        self.assertEqual(listed.data[0].person_id, "u1")
        self.assertEqual(stats["data"]["known"], 1)
        self.assertEqual(detail.data.person_name, "Alice")

        with self.assertRaises(HTTPException) as missing:
            await person_routes.get_person_detail("missing")
        self.assertEqual(missing.exception.status_code, 404)

    async def test_update_person_writes_webui_meta_and_clears_fact_overrides(self) -> None:
        profile = self.store.profiles["u1"]
        profile.facts["person_name"] = "旧事实名"
        profile.facts["nickname"] = "旧昵称"
        profile.facts["name_reason"] = "旧原因"

        updated = await person_routes.update_person(
            "u1",
            person_routes.PersonUpdateRequest(
                person_name=" 新名字 ",
                name_reason="",
                nickname="",
                memory_points=" 新记忆 ",
                is_known=False,
            ),
        )

        meta = profile.stats[person_routes._WEBUI_META_KEY]
        self.assertEqual(updated.message, "用户画像已更新")
        self.assertEqual(updated.data.person_name, "新名字")
        self.assertEqual(meta["person_name"], "新名字")
        self.assertEqual(meta["memory_points"], "新记忆")
        self.assertFalse(meta["is_known"])
        self.assertNotIn("nickname", meta)
        self.assertNotIn("name_reason", meta)
        self.assertNotIn("person_name", profile.facts)
        self.assertNotIn("nickname", profile.facts)
        self.assertNotIn("name_reason", profile.facts)
        self.assertEqual(self.store.saved, ["u1"])

        with self.assertRaises(HTTPException) as missing:
            await person_routes.update_person("missing", person_routes.PersonUpdateRequest(person_name="Nobody"))
        self.assertEqual(missing.exception.status_code, 404)

    async def test_delete_and_batch_delete_routes_report_successes_and_failures(self) -> None:
        batch = await person_routes.batch_delete_persons(
            person_routes.BatchDeleteRequest(person_ids=["u1", "missing", "u2"])
        )

        self.assertEqual(batch.deleted_count, 2)
        self.assertEqual(batch.failed_count, 1)
        self.assertEqual(batch.failed_ids, ["missing"])
        self.assertEqual(self.store.deleted, ["u1", "u2"])

        self.store.profiles["u3"] = make_profile("u3")
        deleted = await person_routes.delete_person("u3")
        self.assertEqual(deleted.message, "用户画像已删除")
        self.assertIn("u3", self.store.deleted)

        with self.assertRaises(HTTPException) as missing:
            await person_routes.delete_person("u3")
        self.assertEqual(missing.exception.status_code, 404)

    async def test_internal_failures_do_not_expose_profile_data_or_paths(self) -> None:
        secret = 'profile secret at /private/user_profiles.db: token="super-secret"'

        with (
            patch.object(person_routes, "_profile_person_page", side_effect=RuntimeError(secret)),
            patch.object(person_routes.logger, "error") as logged,
            self.assertRaises(HTTPException) as failure,
        ):
            await person_routes.get_person_list(
                page=1,
                page_size=20,
                search=None,
                is_known=None,
                platform=None,
            )

        self.assertEqual(failure.exception.status_code, 500)
        self.assertEqual(failure.exception.detail, "获取人物列表失败")
        self.assertNotIn(secret, repr(logged.call_args))


if __name__ == "__main__":
    unittest.main()
