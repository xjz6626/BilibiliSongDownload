import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mutagen.mp4 import MP4

import prv


FFMPEG = shutil.which("ffmpeg")


def create_m4a(path):
    if not FFMPEG:
        raise unittest.SkipTest("ffmpeg is required for real M4A integration tests")
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-t",
            "0.05",
            "-c:a",
            "aac",
            "-y",
            os.fspath(path),
        ],
        check=True,
    )


class ArtistParsingTests(unittest.TestCase):
    def test_scans_full_names_and_aliases_without_duplicates(self):
        artists = prv.scan_artists("【洛天依·言和】测试（洛）")
        self.assertEqual(artists, ["洛天依", "言和"])

    def test_extracts_bvid_without_leaking_it_to_ai_title(self):
        filename = "【洛天依】原创：测试 [BV1Ab411c7DE].m4a"
        with patch("prv.clean_title_with_ai", return_value="测试歌名") as clean_title:
            artists, _, title, output = prv.parse_file_info_hybrid(filename)

        clean_title.assert_called_once_with("【洛天依】原创：测试", "")
        self.assertEqual(artists, ["洛天依"])
        self.assertEqual(title, "测试歌名")
        self.assertEqual(output, "洛天依 - 测试歌名.m4a")
        self.assertNotIn("BV1Ab411c7DE", output)

    def test_different_artists_are_not_name_collisions(self):
        first = prv.build_output_filename(["洛天依"], "同名歌", ".m4a")
        second = prv.build_output_filename(["言和"], "同名歌", ".m4a")
        self.assertEqual(first, "洛天依 - 同名歌.m4a")
        self.assertEqual(second, "言和 - 同名歌.m4a")
        self.assertNotEqual(first, second)

    def test_sanitizes_and_limits_model_output(self):
        title = prv.sanitize_title('  A/B:C?*"<>|\n歌 [BV1Ab411c7DE]  ')
        self.assertEqual(title, "A&B：C？＊＂＜＞｜ 歌")
        filename = prv.build_output_filename([], "很长" * 200, ".m4a")
        self.assertLessEqual(len(filename.encode("utf-8")), prv.MAX_FILENAME_BYTES)

    def test_rejects_empty_model_output(self):
        with self.assertRaisesRegex(ValueError, "空歌名"):
            prv.sanitize_title("\n\t .. ")

    def test_api_failure_is_not_silently_treated_as_success(self):
        with (
            patch.object(prv, "MAX_RETRIES", 1),
            patch("prv.get_client", side_effect=RuntimeError("missing key")),
        ):
            with self.assertRaisesRegex(RuntimeError, "API 请求失败"):
                prv.clean_title_with_ai("测试歌名")

    def test_description_supplements_artists_but_ignores_original_song_reference(self):
        description = "本家：初音未来\nOriginal Vocal: Miku\n演唱：洛天依 / 言和"
        artists = prv.extract_artists_from_sources("《测试歌》", description)
        self.assertEqual(artists, ["洛天依", "言和"])

    def test_title_artists_are_authoritative_over_description_mentions(self):
        artists = prv.extract_artists_from_sources("【洛天依】同名歌", "Vocal：言和")
        self.assertEqual(artists, ["洛天依"])


class SourceMetadataTests(unittest.TestCase):
    def test_reads_title_and_description_from_matching_info_json(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = "文件系统标题 [BV1Ab411c7DE].m4a"
            info_path = Path(directory, "文件系统标题 [BV1Ab411c7DE].info.json")
            info_path.write_text(
                '{"id":"BV1Ab411c7DE","title":"完整视频标题","description":"演唱：洛天依"}',
                encoding="utf-8",
            )

            metadata = prv.load_source_metadata(directory, filename)

            self.assertEqual(metadata["title"], "完整视频标题")
            self.assertEqual(metadata["description"], "演唱:洛天依")
            self.assertEqual(metadata["source_id"], "BV1Ab411c7DE")
            self.assertEqual(metadata["info_path"], os.fspath(info_path))

    def test_reads_uploader_and_bilibili_publish_time(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = "测试 [BV1Ab411c7DE].m4a"
            Path(directory, "测试 [BV1Ab411c7DE].info.json").write_text(
                json.dumps(
                    {
                        "id": "BV1Ab411c7DE",
                        "title": "测试",
                        "uploader": "测试UP主",
                        "uploader_id": 123456,
                        "timestamp": 1704067200,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            metadata = prv.load_source_metadata(directory, filename)

            self.assertEqual(metadata["uploader"], "测试UP主")
            self.assertEqual(metadata["uploader_id"], "123456")
            self.assertEqual(metadata["upload_date"], "2024-01-01")
            self.assertEqual(metadata["publish_time"], "2024-01-01T08:00:00+08:00")

    def test_extracts_stable_bvid_cid_identity_for_multipart_item(self):
        metadata = prv.extract_source_item_metadata(
            {
                "title": "第二分P",
                "formats": [
                    {
                        "url": (
                            "https://example.invalid/upgcxcode/12/34/987654321/"
                            "987654321-1-30280.m4s?token=test"
                        )
                    }
                ],
            },
            "BV1Ab411c7DE_p2",
        )

        self.assertEqual(metadata["bvid"], "BV1Ab411c7DE")
        self.assertEqual(metadata["cid"], "987654321")
        self.assertEqual(metadata["page"], "2")
        self.assertEqual(metadata["part_title"], "第二分P")
        self.assertEqual(
            metadata["source_key"],
            "bvid:bv1ab411c7de:cid:987654321",
        )

    def test_rejects_info_json_for_a_different_video(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = "测试 [BV1Ab411c7DE].m4a"
            Path(directory, "测试 [BV1Ab411c7DE].info.json").write_text(
                '{"id":"BV1WrongVideo","title":"错误视频"}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "视频 ID 不一致"):
                prv.load_source_metadata(directory, filename)


class CoverDetectionTests(unittest.TestCase):
    def test_detects_strong_cover_signals_from_favorite_history(self):
        samples = [
            "【乐正龙牙翻唱】大眠·让我陪他恋完这场爱【原创PV付】",
            "【Vsinger全员】《爱如阳光》（ACE Cover）【原创PV付】",
            "【洛天依日文翻唱】Lemon【动点p】",
            "【海伊AI】《Traveling Light》（SV Cover）【原创PV付】",
            "【vsinger全员+UTAU3人】DAZE【中文填词】【COVER】",
            "【洛天依XS】冰糖葫芦2.0【重编曲翻唱】",
            "高ping战士（虚拟歌手全网首翻）",
            "中文填词翻调！卖火柴的倒爷",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(prv.detect_cover(sample))

    def test_does_not_treat_related_but_non_cover_signals_as_cover(self):
        samples = [
            "【本家投稿】Mayonazy - Teeth Mark Love (牙印为爱) feat. GUMI",
            "【初投稿/洛天依】《穹光》（star sky中文翻填）",
            "Doomer 中文填词【東京真中】【重音テトSV】",
            "誓见（挚友Remix）中文填词",
            "【星尘原创曲】星轨航向标",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertFalse(prv.detect_cover(sample))

    def test_cover_mark_is_part_of_full_name_before_duplicate_numbering(self):
        original = prv.build_output_filename(["洛天依"], "同名歌", ".m4a", False)
        cover = prv.build_output_filename(["洛天依"], "同名歌", ".m4a", True)
        second_cover = prv._numbered_filename(cover, 2)

        self.assertEqual(original, "洛天依 - 同名歌.m4a")
        self.assertEqual(cover, "洛天依 - 同名歌 [翻唱].m4a")
        self.assertEqual(second_cover, "洛天依 - 同名歌 [翻唱] (2).m4a")

    def test_description_cover_signal_ignores_original_song_reference(self):
        self.assertFalse(prv.detect_cover_from_sources("测试歌", "本家：Example (Cover)"))
        self.assertTrue(prv.detect_cover_from_sources("测试歌", "本视频为洛天依翻唱"))

    def test_description_cover_signal_ignores_permissions_and_materials(self):
        samples = [
            "【禁】一切翻唱，感谢配合",
            "若需翻唱翻调授权请私信",
            "暂不开放虚拟歌姬翻唱授权",
            "翻唱用素材下载请点这里",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertFalse(prv.detect_cover_from_sources("原创歌曲", sample))

    def test_staff_alone_is_not_cover_but_explicit_cover_version_is(self):
        self.assertFalse(prv.detect_cover_from_sources("测试歌", "Staff：曲/词/编/调/混/PV"))
        self.assertTrue(prv.detect_cover_from_sources("测试歌", "言和翻调版Staff："))


class CollisionTests(unittest.TestCase):
    def test_legacy_collision_gets_stable_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "歌名.m4a").touch()
            result = prv.resolve_output_filename(directory, "另一个原标题.m4a", "歌名.m4a")
            self.assertEqual(result, "歌名 (2).m4a")
            self.assertEqual(
                result,
                prv.resolve_output_filename(directory, "另一个原标题.m4a", "歌名.m4a"),
            )

    def test_no_overwrite_rename_preserves_both_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "source.m4a")
            target = Path(directory, "target.m4a")
            source.write_bytes(b"source")
            target.write_bytes(b"target")

            with self.assertRaises(FileExistsError):
                prv.rename_without_overwrite(source, target)

            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(target.read_bytes(), b"target")


@unittest.skipUnless(FFMPEG, "ffmpeg is required for real M4A integration tests")
class AtomicMetadataTests(unittest.TestCase):
    def test_existing_v2_state_without_new_attribution_fields_stays_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "旧版完整状态.m4a")
            create_m4a(source)
            state = {
                "version": prv.LEGACY_STATE_VERSION,
                "source_name": source.name,
                "source_id": "BV1Ab411c7DE",
                "final_name": source.name,
                "title": "旧版完整状态",
                "artists": ["洛天依"],
                "is_cover": False,
                "cover_embedded": False,
                "description_sha256": "",
            }
            audio = MP4(source)
            prv._write_metadata(
                audio,
                state,
                "旧版完整标题",
                "",
                ["洛天依"],
                "旧版完整状态",
                None,
            )
            audio.save()

            self.assertEqual(prv.read_processing_state(source), state)
            self.assertTrue(prv.already_processed(source))

    def test_processes_real_m4a_and_writes_recovery_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "【洛天依原创】完整视频标题 [BV1Ab411c7DE].m4a")
            create_m4a(source)
            cover = source.with_suffix(".png")
            cover.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ))

            with patch("prv.clean_title_with_ai", return_value="清洗后歌名"):
                final_name = prv.process_file(directory, source.name)

            target = Path(directory, final_name)
            self.assertFalse(source.exists())
            self.assertFalse(cover.exists())
            self.assertTrue(target.exists())
            self.assertEqual(final_name, "洛天依 - 清洗后歌名.m4a")
            self.assertNotIn("BV", final_name)

            audio = MP4(target)
            self.assertEqual(audio["\xa9nam"], ["清洗后歌名"])
            self.assertEqual(audio["\xa9cmt"], ["【洛天依原创】完整视频标题"])
            self.assertEqual(audio["\xa9ART"], ["洛天依"])
            self.assertTrue(audio["covr"])
            self.assertTrue(prv.already_processed(target))
            self.assertEqual(prv.read_processing_state(target)["source_id"], "BV1Ab411c7DE")

    def test_cover_label_is_written_to_filename_title_grouping_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "【乐正龙牙翻唱】大眠【原创PV付】 [BV1XyHNzVERz].m4a")
            create_m4a(source)

            with patch("prv.clean_title_with_ai", return_value="大眠"):
                final_name = prv.process_file(directory, source.name)

            self.assertEqual(final_name, "乐正龙牙 - 大眠 [翻唱].m4a")
            audio = MP4(Path(directory, final_name))
            self.assertEqual(audio["\xa9nam"], ["大眠 [翻唱]"])
            self.assertEqual(audio["\xa9grp"], ["翻唱"])
            state = prv.read_processing_state(Path(directory, final_name))
            self.assertTrue(state["is_cover"])

    def test_info_json_title_and_description_feed_extraction_then_are_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "截断标题 [BV1Ab411c7DE].m4a")
            create_m4a(source)
            description = "演唱：洛天依\n本视频为翻唱作品"
            info_path = source.with_suffix(".info.json")
            info_path.write_text(
                json.dumps(
                    {
                        "id": "BV1Ab411c7DE",
                        "title": "《完整歌名》投稿",
                        "description": description,
                        "uploader": "测试UP主",
                        "uploader_id": "98765",
                        "upload_date": "20240102",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("prv.clean_title_with_ai", return_value="完整歌名") as clean_title:
                final_name = prv.process_file(directory, source.name)

            clean_title.assert_called_once_with("《完整歌名》投稿", "演唱:洛天依\n本视频为翻唱作品")
            self.assertEqual(final_name, "洛天依 - 完整歌名 [翻唱].m4a")
            self.assertFalse(info_path.exists())
            audio = MP4(Path(directory, final_name))
            self.assertEqual(audio["\xa9ART"], ["洛天依"])
            self.assertEqual(audio["\xa9cmt"], ["《完整歌名》投稿"])
            stored_description = bytes(audio[prv.SOURCE_DESCRIPTION_TAG][0]).decode("utf-8")
            self.assertEqual(stored_description, "演唱:洛天依\n本视频为翻唱作品")
            self.assertEqual(bytes(audio[prv.SOURCE_UPLOADER_TAG][0]).decode("utf-8"), "测试UP主")
            self.assertEqual(bytes(audio[prv.SOURCE_UPLOADER_ID_TAG][0]).decode("utf-8"), "98765")
            self.assertEqual(
                bytes(audio[prv.SOURCE_PUBLISH_TIME_TAG][0]).decode("utf-8"),
                "2024-01-02",
            )
            self.assertEqual(audio["\xa9day"], ["2024-01-02"])
            state = prv.read_processing_state(Path(directory, final_name))
            self.assertEqual(
                state["description_sha256"],
                hashlib.sha256(stored_description.encode("utf-8")).hexdigest(),
            )

    def test_v3_writes_structured_staff_and_preserves_unmanaged_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "完整Staff [BV1Ab411c7DE].m4a")
            create_m4a(source)
            audio = MP4(source)
            if audio.tags is None:
                audio.add_tags()
            audio["\xa9lyr"] = ["这是歌词正文"]
            unmanaged_tag = "----:com.apple.iTunes:UserPrivateTag"
            audio[unmanaged_tag] = [b"keep-me"]
            audio.save()
            source.with_suffix(".info.json").write_text(
                json.dumps(
                    {
                        "id": "BV1Ab411c7DE",
                        "title": "《完整Staff》",
                        "description": (
                            "作词：甲、乙\n作曲：丙\n编曲：丁\n调教：戊\n"
                            "混音：己\n曲绘：庚\nPV：辛"
                        ),
                        "uploader": "投稿UP",
                        "uploader_id": "42",
                        "cid": 987654321,
                        "timestamp": 1704067200,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("prv.clean_title_with_ai", return_value="完整Staff"):
                final_name = prv.process_file(directory, source.name)

            target = Path(directory, final_name)
            audio = MP4(target)
            self.assertEqual(audio["\xa9lyr"], ["这是歌词正文"])
            self.assertEqual(bytes(audio[unmanaged_tag][0]), b"keep-me")
            self.assertEqual(audio["\xa9wrt"], ["丙"])
            self.assertEqual(
                [bytes(value).decode("utf-8") for value in audio["----:com.apple.iTunes:BilibiliLyricist"]],
                ["甲", "乙"],
            )
            self.assertEqual(
                bytes(audio[prv.SOURCE_ORIGINAL_TITLE_TAG][0]).decode("utf-8"),
                "《完整Staff》",
            )
            self.assertEqual(bytes(audio[prv.SOURCE_ID_TAG][0]).decode("utf-8"), "BV1Ab411c7DE")
            self.assertEqual(bytes(audio[prv.SOURCE_CID_TAG][0]).decode("utf-8"), "987654321")
            state = prv.read_processing_state(target)
            self.assertEqual(state["version"], prv.STATE_VERSION)
            self.assertEqual(state["source_key"], "bvid:bv1ab411c7de:cid:987654321")
            self.assertEqual(state["credits_schema"], 1)

    def test_failed_processing_keeps_info_json_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "原始标题 [BV1Ab411c7DE].m4a")
            create_m4a(source)
            info_path = source.with_suffix(".info.json")
            info_path.write_text(
                '{"id":"BV1Ab411c7DE","title":"原始标题","description":"演唱：洛天依"}',
                encoding="utf-8",
            )

            with (
                patch("prv.clean_title_with_ai", return_value="歌名"),
                patch("prv._write_metadata", side_effect=RuntimeError("injected failure")),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    prv.process_file(directory, source.name)

            self.assertTrue(source.exists())
            self.assertTrue(info_path.exists())

    def test_failure_before_atomic_replace_keeps_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "原始标题 [BV1Ab411c7DE].m4a")
            create_m4a(source)
            original_bytes = source.read_bytes()

            with (
                patch("prv.clean_title_with_ai", return_value="歌名"),
                patch("prv._write_metadata", side_effect=RuntimeError("injected failure")),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    prv.process_file(directory, source.name)

            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertEqual(list(Path(directory).glob(f"{prv.TEMP_PREFIX}*")), [])

    def test_crash_after_replace_recovers_rename_without_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "原始标题 [BV1Ab411c7DE].m4a")
            create_m4a(source)
            final_name = "歌手 - 歌名.m4a"

            with (
                patch(
                    "prv.parse_file_info_hybrid",
                    return_value=(["歌手"], "歌手", "歌名", final_name),
                ),
                patch("prv.rename_without_overwrite", side_effect=KeyboardInterrupt),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    prv.process_file(directory, source.name)

            self.assertTrue(source.exists())
            self.assertFalse(Path(directory, final_name).exists())
            self.assertFalse(prv.already_processed(source))

            with patch("prv.clean_title_with_ai") as ai:
                action, recovered_path = prv.recover_completed_rename(source)

            self.assertEqual(action, "recovered")
            self.assertEqual(Path(recovered_path).name, final_name)
            self.assertTrue(Path(recovered_path).exists())
            self.assertFalse(source.exists())
            ai.assert_not_called()

    def test_same_artist_and_title_number_across_batches_without_visible_bvid(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory, "版本甲 [BV1Ab411c7DE].m4a")
            second = Path(directory, "版本乙 [BV1Xx411c7YY].m4a")
            create_m4a(first)
            create_m4a(second)

            with patch("prv.clean_title_with_ai", return_value="同名歌"):
                first_name = prv.process_file(directory, first.name)
                # 模拟第一首已经推送并从 Music_Done 删除；注册表仍必须记住占用名。
                Path(directory, first_name).unlink()
                second_name = prv.process_file(directory, second.name)

            self.assertEqual(first_name, "同名歌.m4a")
            self.assertEqual(second_name, "同名歌 (2).m4a")
            self.assertNotIn("BV", first_name + second_name)
            self.assertFalse(Path(directory, first_name).exists())
            self.assertTrue(Path(directory, second_name).exists())

    def test_name_reservation_survives_processing_failure_without_second_ai_call(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "原始标题 [BV1Ab411c7DE].m4a")
            create_m4a(source)

            with (
                patch("prv.clean_title_with_ai", return_value="稳定歌名"),
                patch("prv._write_metadata", side_effect=RuntimeError("injected failure")),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    prv.process_file(directory, source.name)

            with patch("prv.clean_title_with_ai") as ai:
                final_name = prv.process_file(directory, source.name)

            ai.assert_not_called()
            self.assertEqual(final_name, "稳定歌名.m4a")
            self.assertTrue(Path(directory, final_name).exists())

    def test_multipart_cid_does_not_reuse_legacy_bvid_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            prv.reserve_name_assignment(
                directory,
                "旧单P.m4a",
                "BV1Ab411c7DE",
                "旧单P.m4a",
                "旧单P",
                [],
                False,
            )
            source = Path(directory, "第二分P [BV1Ab411c7DE_p2].m4a")
            create_m4a(source)
            source.with_suffix(".info.json").write_text(
                json.dumps(
                    {
                        "id": "BV1Ab411c7DE_p2",
                        "cid": 222,
                        "playlist_index": 2,
                        "title": "第二分P新歌",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("prv.clean_title_with_ai", return_value="分P新歌") as clean_title:
                final_name = prv.process_file(directory, source.name)

            clean_title.assert_called_once()
            self.assertEqual(final_name, "分P新歌.m4a")
            state = prv.read_processing_state(Path(directory, final_name))
            self.assertEqual(state["source_key"], "bvid:bv1ab411c7de:cid:222")

    def test_main_is_idempotent_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "【洛天依】测试 [BV1Ab411c7DE].m4a")
            create_m4a(source)
            report = Path(directory, "report.log")

            with (
                patch.object(prv, "MUSIC_DIR", directory),
                patch.object(prv, "REPORT_FILE", os.fspath(report)),
                patch("prv.clean_title_with_ai", return_value="测试"),
            ):
                self.assertEqual(prv.main(), 0)

            with (
                patch.object(prv, "MUSIC_DIR", directory),
                patch.object(prv, "REPORT_FILE", os.fspath(report)),
                patch("prv.clean_title_with_ai") as ai,
            ):
                self.assertEqual(prv.main(), 0)
                ai.assert_not_called()

            self.assertEqual(len(list(Path(directory).glob("*.m4a"))), 1)
            self.assertGreaterEqual(report.read_text(encoding="utf-8").count("=== "), 4)

    def test_old_album_tags_without_state_are_not_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "旧版半成品.m4a")
            create_m4a(source)
            audio = MP4(source)
            if audio.tags is None:
                audio.add_tags()
            audio["\xa9nam"] = ["歌名"]
            audio["\xa9alb"] = [prv.TARGET_ALBUM]
            audio["aART"] = [prv.TARGET_ALBUM_ARTIST]
            audio.save()

            self.assertFalse(prv.already_processed(source))


class LockTests(unittest.TestCase):
    def test_rejects_second_processor(self):
        with tempfile.TemporaryDirectory() as directory:
            with prv.processing_lock(directory):
                with self.assertRaisesRegex(RuntimeError, "另一个 prv.py"):
                    with prv.processing_lock(directory):
                        pass


if __name__ == "__main__":
    unittest.main()
