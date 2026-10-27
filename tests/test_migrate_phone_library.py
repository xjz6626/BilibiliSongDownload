import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mutagen.mp4 import MP4

import migrate_phone_library as migration


FFMPEG = shutil.which("ffmpeg")


def box(kind, payload, *, extended=False):
    if extended:
        return struct.pack(">I4sQ", 1, kind, len(payload) + 16) + payload
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def create_m4a(path):
    if not FFMPEG:
        raise unittest.SkipTest("ffmpeg is required for M4A integration tests")
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
    audio = MP4(path)
    audio["\xa9nam"] = ["旧歌名"]
    audio["\xa9ART"] = ["旧歌姬"]
    audio["\xa9lyr"] = ["用户保留的歌词"]
    audio["----:com.apple.iTunes:UserPrivateTag"] = [b"keep-user-value"]
    audio["covr"] = [b"same-cover"]
    audio.save()


def catalog_record(**overrides):
    record = {
        "source_key": "bvid:bv1ab411c7de:cid:101",
        "source_item_id": "BV1Ab411c7DE_p1",
        "bvid": "BV1Ab411c7DE",
        "cid": "101",
        "part": 1,
        "part_count": 2,
        "title": "【洛天依】完整标题",
        "description": "演唱：洛天依\n词：甲\n曲：乙",
        "uploader": "测试UP",
        "uploader_id": "123",
        "upload_date": "2024-01-01",
        "publish_time": "2024-01-01T08:00:00+08:00",
        "duration": 1.0,
        "cover_sha256": "cover-one",
        "cover_path": "",
        "info_path": "",
    }
    record.update(overrides)
    return record


class ArchiveAndCatalogTests(unittest.TestCase):
    def test_archive_retains_parts_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory, "archive.txt")
            archive.write_text(
                "bilibili BV1Ab411c7DE\n"
                "bilibili BV1Ab411c7DE_p2\n"
                "bilibili BV1Ab411c7DE_p2\n"
                "bad line\n",
                encoding="utf-8",
            )
            self.assertEqual(
                migration.parse_archive(archive),
                [
                    {"bvid": "BV1Ab411c7DE", "part": None, "source_key": "BV1Ab411c7DE"},
                    {"bvid": "BV1Ab411c7DE", "part": 2, "source_key": "BV1Ab411c7DE_p2"},
                ],
            )

    def test_catalog_uses_bvid_and_cid_as_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory)
            cover = catalog / "BV1Ab411c7DE_p2.jpg"
            cover.write_bytes(b"cover")
            Path(catalog, "BV1Ab411c7DE_p2.info.json").write_text(
                json.dumps(
                    {
                        "id": "BV1Ab411c7DE_p2",
                        "cid": 98765,
                        "playlist_index": 2,
                        "playlist_count": 3,
                        "title": "第二分P",
                        "description": "简介",
                    }
                ),
                encoding="utf-8",
            )
            records = migration.load_catalog(catalog)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source_key"], "bvid:bv1ab411c7de:cid:98765")
            self.assertEqual(records[0]["cid"], "98765")
            self.assertEqual(records[0]["part"], 2)
            self.assertEqual(records[0]["cover_sha256"], hashlib.sha256(b"cover").hexdigest())

    def test_playlist_entries_use_their_own_part_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory)
            parts = []
            for page, cid, cover_bytes in ((1, 101, b"cover-one"), (2, 202, b"cover-two")):
                item_id = f"BV1Ab411c7DE_p{page}"
                metadata = {
                    "id": item_id,
                    "cid": cid,
                    "playlist_index": page,
                    "playlist_count": 2,
                    "title": f"第{page}分P",
                    "description": "简介",
                }
                parts.append(metadata)
                Path(catalog, f"{item_id}.info.json").write_text(
                    json.dumps(metadata), encoding="utf-8"
                )
                Path(catalog, f"{item_id}.jpg").write_bytes(cover_bytes)
            Path(catalog, "BV1Ab411c7DE.info.json").write_text(
                json.dumps({"id": "BV1Ab411c7DE", "entries": parts}),
                encoding="utf-8",
            )

            records = migration.load_catalog(catalog)

            self.assertEqual(
                {record["cid"]: record["cover_sha256"] for record in records},
                {
                    "101": hashlib.sha256(b"cover-one").hexdigest(),
                    "202": hashlib.sha256(b"cover-two").hexdigest(),
                },
            )

    def test_fetch_catalog_never_requests_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory, "archive.txt")
            archive.write_text("bilibili BV1Ab411c7DE\n", encoding="utf-8")
            with (
                patch("migrate_phone_library.subprocess.run") as run,
                patch("migrate_phone_library.load_catalog", return_value=[catalog_record()]),
            ):
                run.return_value.returncode = 0
                migration.fetch_catalog(archive, Path(directory, "catalog"))
            command = run.call_args.args[0]
            self.assertIn("--skip-download", command)
            self.assertIn("--yes-playlist", command)
            self.assertNotIn("-f", command)
            self.assertNotIn("--extract-audio", command)


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.first = catalog_record(cover_sha256="shared")
        self.second = catalog_record(
            source_key="bvid:bv1ab411c7de:cid:202",
            source_item_id="BV1Ab411c7DE_p2",
            cid="202",
            part=2,
            cover_sha256="shared",
        )

    def test_bvid_and_cid_match_exact_part(self):
        result = migration.match_track(
            {"source_id": "BV1Ab411c7DE_p1", "source_cid": "202", "cover_sha256": ""},
            [self.first, self.second],
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["method"], "embedded_bvid_cid")
        self.assertEqual(result["record"]["cid"], "202")

    def test_page_number_without_cid_is_never_part_identity(self):
        result = migration.match_track(
            {"source_id": "BV1Ab411c7DE_p2", "source_cid": "", "cover_sha256": ""},
            [self.first, self.second],
        )
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["method"], "embedded_bvid_missing_or_unknown_cid")

    def test_conflicting_embedded_cid_never_falls_back_to_bvid(self):
        only = catalog_record(part=None, part_count=None, cid="101")
        result = migration.match_track(
            {"source_id": "BV1Ab411c7DE", "source_cid": "999", "cover_sha256": ""},
            [only],
        )

        self.assertEqual(result["status"], "review")
        self.assertEqual(result["method"], "embedded_bvid_cid_not_in_catalog")

    def test_even_one_loaded_multipart_record_needs_cid_for_bvid_match(self):
        result = migration.match_track(
            {"source_id": "BV1Ab411c7DE_p1", "source_cid": "", "cover_sha256": ""},
            [self.first],
        )
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["method"], "embedded_bvid_multipart_missing_cid")

    def test_same_cover_across_parts_is_always_review(self):
        result = migration.match_track(
            {"source_id": "", "source_cid": "", "cover_sha256": "shared"},
            [self.first, self.second],
        )
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["method"], "cover_sha256_multiple_candidates")

    def test_single_cover_needs_compatible_duration(self):
        single = catalog_record(
            cover_sha256="only", duration=102.0, part=None, part_count=None
        )
        matched = migration.match_track(
            {"source_id": "", "source_cid": "", "cover_sha256": "only", "duration": 100.0},
            [single],
        )
        self.assertEqual(matched["status"], "matched")
        self.assertEqual(matched["method"], "cover_sha256_duration")

        for phone_duration in (None, 160.0):
            with self.subTest(phone_duration=phone_duration):
                review = migration.match_track(
                    {"source_id": "", "source_cid": "", "cover_sha256": "only", "duration": phone_duration},
                    [catalog_record(
                        cover_sha256="only", duration=100.0, part=None, part_count=None
                    )],
                )
                self.assertEqual(review["status"], "review")

    def test_zero_duration_needs_unique_cover_and_specific_verbatim_title(self):
        record = catalog_record(
            title="【洛天依原创】同一首歌【PV付】",
            cover_sha256="only",
            duration=210.0,
            part=None,
            part_count=None,
        )
        matched = migration.match_track(
            {
                "source_id": "",
                "source_cid": "",
                "cover_sha256": "only",
                "duration": 0.0,
                "title": "同一首歌",
            },
            [record],
        )
        self.assertEqual(matched["status"], "matched")
        self.assertEqual(matched["method"], "cover_sha256_zero_duration_title")

        for title in ("另一首歌", "光"):
            with self.subTest(title=title):
                review = migration.match_track(
                    {
                        "source_id": "",
                        "source_cid": "",
                        "cover_sha256": "only",
                        "duration": 0.0,
                        "title": title,
                    },
                    [record],
                )
                self.assertEqual(review["status"], "review")

    def test_real_nonzero_duration_conflict_is_not_overridden_by_title(self):
        result = migration.match_track(
            {
                "source_id": "",
                "source_cid": "",
                "cover_sha256": "only",
                "duration": 120.0,
                "title": "同一首歌",
            },
            [catalog_record(
                title="【原创】同一首歌",
                cover_sha256="only",
                duration=220.0,
                part=None,
                part_count=None,
            )],
        )
        self.assertEqual(result["status"], "review")

    def test_single_loaded_multipart_cover_is_not_an_automatic_match(self):
        result = migration.match_track(
            {"source_id": "", "source_cid": "", "cover_sha256": "only", "duration": 100.0},
            [catalog_record(cover_sha256="only", duration=100.0, part=1, part_count=3)],
        )

        self.assertEqual(result["status"], "review")
        self.assertEqual(result["method"], "cover_sha256_multipart_catalog_incomplete")

    def test_ambiguous_catalog_title_cannot_create_delete_candidate(self):
        track = {
            "remote_path": "/phone/普通歌曲.m4a",
            "filename": "普通歌曲.m4a",
            "source_id": "",
            "source_cid": "",
            "cover_sha256": "shared",
            "title": "普通歌曲",
            "duration": 100.0,
        }
        first = catalog_record(title="钢琴伴奏版", cover_sha256="shared")
        second = catalog_record(
            source_key="bvid:bv1ab411c7de:cid:202", source_item_id="BV1Ab411c7DE_p2",
            cid="202", part=2, title="正式演唱版", cover_sha256="shared",
        )
        plan = migration.build_plan(
            [track], [first, second], remote_dir="/phone/", adb_serial="serial-1", archive_sha256="a" * 64
        )
        self.assertEqual(plan["entries"][0]["action"], "review")
        self.assertEqual(plan["entries"][0]["instrumental_evidence"]["matched_catalog"], [])

    def test_instrumentals_are_candidates_not_automatic_actions(self):
        self.assertIn("伴奏", migration.instrumental_evidence("歌名 钢琴伴奏版"))
        self.assertIn("off vocal", migration.instrumental_evidence("Song (Off Vocal)"))
        self.assertIn("inst", migration.instrumental_evidence("p02 inst. only"))


class IntegrityTests(unittest.TestCase):
    def test_audio_hash_only_covers_mdat_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory, "first.m4a")
            second = Path(directory, "second.m4a")
            first.write_bytes(box(b"ftyp", b"metadata-a") + box(b"mdat", b"audio", extended=True))
            second.write_bytes(box(b"ftyp", b"metadata-b-is-different") + box(b"mdat", b"audio", extended=True))
            self.assertEqual(migration.hash_audio_stream(first), migration.hash_audio_stream(second))
            self.assertEqual(
                migration.hash_audio_stream(first), hashlib.sha256(b"audio").hexdigest()
            )

    def test_plan_hash_detects_any_edit(self):
        plan = migration.seal_plan({"version": 1, "entries": [], "summary": {"update": 0}})
        migration.validate_plan(plan, plan["plan_hash"])
        plan["summary"]["update"] = 1
        with self.assertRaisesRegex(migration.MigrationError, "已改变"):
            migration.validate_plan(plan)

    def test_scan_releases_each_pulled_temp_file_immediately(self):
        class ReadOnlyADB:
            def list_files(self, _remote_dir):
                return [f"/phone/{index}.m4a" for index in range(3)]

            def pull(self, _remote, local):
                local = Path(local)
                self.assert_no_previous(local.parent)
                local.write_bytes(b"temporary")

            @staticmethod
            def assert_no_previous(directory):
                if list(directory.glob("*.m4a")):
                    raise AssertionError("上一首扫描临时文件未及时删除")

        with patch(
            "migrate_phone_library.inspect_m4a",
            side_effect=lambda local, remote: {"remote_path": remote, "size": Path(local).stat().st_size},
        ):
            tracks = migration.scan_phone(ReadOnlyADB(), "/phone/")

        self.assertEqual(len(tracks), 3)

    def test_llm_title_cache_is_persisted_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory, "titles.json")
            cache = {}
            with patch("migrate_phone_library.prv.clean_title_with_ai", return_value="清洗歌名") as clean:
                first = migration.clean_title_cached("原标题", "简介", cache, cache_path)
                second = migration.clean_title_cached("原标题", "简介", cache, cache_path)
            self.assertEqual((first, second), ("清洗歌名", "清洗歌名"))
            clean.assert_called_once_with("原标题", "简介")
            self.assertEqual(migration.load_title_cache(cache_path), cache)

    def test_credit_extraction_error_forces_review(self):
        track = {
            "remote_path": "/phone/歌曲.m4a", "filename": "歌曲.m4a", "source_id": "BV1Ab411c7DE",
            "source_cid": "101", "cover_sha256": "", "title": "歌曲", "artists": [],
        }
        with patch("migrate_phone_library._extract_credits", side_effect=ValueError("bad staff")):
            plan = migration.build_plan(
                [track], [catalog_record()], remote_dir="/phone/", adb_serial="serial-1", archive_sha256="a" * 64
            )
        self.assertEqual(plan["entries"][0]["action"], "review")
        self.assertEqual(plan["entries"][0]["review_reason"], "credit_extraction_error")

    def test_plan_pilot_only_calls_llm_for_released_updates(self):
        tracks = [
            {
                "remote_path": f"/phone/歌曲{index}.m4a",
                "filename": f"歌曲{index}.m4a",
                "source_id": f"BV1Ab411c7D{index}",
                "source_cid": str(100 + index),
                "cover_sha256": "",
                "title": f"旧标题{index}",
                "artists": [],
            }
            for index in (1, 2)
        ]
        records = [
            catalog_record(
                bvid=f"BV1Ab411c7D{index}",
                cid=str(100 + index),
                source_key=f"bvid:bv1ab411c7d{index}:cid:{100 + index}",
                source_item_id=f"BV1Ab411c7D{index}",
                part=None,
                part_count=None,
                title=f"完整标题{index}",
            )
            for index in (1, 2)
        ]

        with patch("migrate_phone_library.clean_title_cached", return_value="清洗后") as clean:
            plan = migration.build_plan(
                tracks,
                records,
                remote_dir="/phone/",
                adb_serial="serial-1",
                archive_sha256="a" * 64,
                use_llm=True,
                max_updates=1,
            )

        clean.assert_called_once()
        self.assertEqual(plan["entries"][0]["action"], "update")
        self.assertEqual(plan["entries"][0]["proposed"]["title"], "清洗后")
        self.assertEqual(plan["entries"][1]["action"], "review")
        self.assertEqual(plan["entries"][1]["review_reason"], "pilot_limit")


class FakeADB:
    def __init__(self, serial, root, remote_dir="/phone/"):
        self.serial = serial
        self.root = Path(root)
        self.remote_dir = remote_dir.rstrip("/")
        self.pushed_remotes = []

    def local(self, remote):
        return self.root / Path(remote).name

    def list_files(self, remote_dir):
        return sorted(
            f"{self.remote_dir}/{path.name}"
            for path in self.root.iterdir()
            if path.is_file() and path.suffix.lower() == ".m4a" and not path.name.startswith(".bilibili-migrate-")
        )

    def pull(self, remote, local):
        shutil.copy2(self.local(remote), local)

    def push(self, local, remote):
        self.pushed_remotes.append(remote)
        shutil.copy2(local, self.local(remote))

    def exists(self, remote):
        return self.local(remote).exists()

    def remote_sha256(self, remote):
        return migration.sha256_file(self.local(remote))

    def atomic_replace(self, source, temp, target, source_sha256, temp_sha256):
        if self.remote_sha256(source) != source_sha256:
            raise migration.MigrationError("source changed")
        if self.remote_sha256(temp) != temp_sha256:
            raise migration.MigrationError("temp changed")
        source_local, temp_local, target_local = self.local(source), self.local(temp), self.local(target)
        if source_local != target_local and target_local.exists():
            raise migration.MigrationError("target exists")
        os.replace(temp_local, target_local)
        if source_local != target_local:
            source_local.unlink()

    def delete_verified(self, remote, expected_sha256):
        if self.remote_sha256(remote) != expected_sha256:
            raise migration.MigrationError("source changed")
        self.local(remote).unlink()


class CorruptReadbackADB(FakeADB):
    def __init__(self, serial, root, remote_dir="/phone/"):
        super().__init__(serial, root, remote_dir)
        self.replaced = False

    def atomic_replace(self, *args):
        super().atomic_replace(*args)
        self.replaced = True

    def pull(self, remote, local):
        super().pull(remote, local)
        if self.replaced:
            with open(local, "ab") as handle:
                handle.write(b"corrupt-transfer")


@unittest.skipUnless(FFMPEG, "ffmpeg is required for M4A integration tests")
class ApplyTests(unittest.TestCase):
    def make_plan(self, device_dir, title="【洛天依】完整标题", rename=False):
        source = Path(device_dir, "旧文件.m4a")
        create_m4a(source)
        track = migration.inspect_m4a(source, "/phone/旧文件.m4a")
        record = catalog_record(
            title=title,
            cover_sha256=track["cover_sha256"],
            duration=track["duration"],
            part=None,
            part_count=None,
            source_item_id="BV1Ab411c7DE",
        )
        return migration.build_plan(
            [track],
            [record],
            remote_dir="/phone/",
            adb_serial="serial-1",
            archive_sha256="a" * 64,
            rename_files=rename,
        )

    def test_apply_backs_up_and_preserves_audio_stream(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as backups:
            plan = self.make_plan(directory)
            original_stream = plan["entries"][0]["phone"]["audio_stream_sha256"]
            fake = FakeADB("serial-1", directory)
            result = migration.apply_plan(
                plan,
                confirmed_hash=plan["plan_hash"],
                backup_dir=backups,
                adb=fake,
            )
            self.assertEqual(result, {"updated": 1})
            self.assertEqual(len(fake.pushed_remotes), 1)
            self.assertTrue(fake.pushed_remotes[0].endswith(".part"))
            self.assertNotIn("/.bilibili-migrate-", fake.pushed_remotes[0])
            final = Path(directory, "旧文件.m4a")
            self.assertEqual(migration.hash_audio_stream(final), original_stream)
            self.assertEqual(
                migration._decode_freeform(MP4(final).tags, migration.SOURCE_CID_TAG), "101"
            )
            final_audio = MP4(final)
            self.assertEqual(final_audio["\xa9lyr"], ["用户保留的歌词"])
            self.assertEqual(bytes(final_audio["covr"][0]), b"same-cover")
            self.assertEqual(
                bytes(final_audio["----:com.apple.iTunes:UserPrivateTag"][0]),
                b"keep-user-value",
            )
            credits = json.loads(
                migration._decode_freeform(final_audio.tags, migration.prv.SOURCE_CREDITS_TAG)
            )
            self.assertEqual(credits["lyricists"], ["甲"])
            self.assertEqual(credits["composers"], ["乙"])
            backup = Path(backups, plan["plan_hash"][:16], "旧文件.m4a")
            self.assertTrue(backup.exists())
            self.assertEqual(migration.sha256_file(backup), plan["entries"][0]["phone"]["file_sha256"])

    def test_rename_needs_second_explicit_permission(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as backups:
            plan = self.make_plan(directory, rename=True)
            fake = FakeADB("serial-1", directory)
            self.assertNotEqual(
                plan["entries"][0]["phone"]["filename"],
                plan["entries"][0]["proposed"]["target_filename"],
            )
            with self.assertRaisesRegex(migration.MigrationError, "--allow-rename"):
                migration.apply_plan(
                    plan,
                    confirmed_hash=plan["plan_hash"],
                    backup_dir=backups,
                    adb=fake,
                )
            self.assertTrue(Path(directory, "旧文件.m4a").exists())

    def test_apply_repulls_and_rejects_bad_final_file(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as backups:
            plan = self.make_plan(directory)
            fake = CorruptReadbackADB("serial-1", directory)
            with self.assertRaisesRegex(migration.MigrationError, "回读后的整文件"):
                migration.apply_plan(
                    plan,
                    confirmed_hash=plan["plan_hash"],
                    backup_dir=backups,
                    adb=fake,
                )
            self.assertTrue(Path(backups, plan["plan_hash"][:16], "旧文件.m4a").exists())

    def test_deletion_is_separate_and_needs_flag(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as backups:
            plan = self.make_plan(directory, title="钢琴伴奏版")
            self.assertEqual(plan["entries"][0]["action"], "delete_candidate")
            fake = FakeADB("serial-1", directory)
            self.assertEqual(
                migration.apply_plan(
                    plan,
                    confirmed_hash=plan["plan_hash"],
                    backup_dir=backups,
                    adb=fake,
                ),
                {},
            )
            self.assertTrue(Path(directory, "旧文件.m4a").exists())
            with self.assertRaisesRegex(migration.MigrationError, "--allow-delete"):
                migration.delete_candidates(
                    plan,
                    confirmed_hash=plan["plan_hash"],
                    backup_dir=backups,
                    allow_delete=False,
                    adb=fake,
                )
            self.assertEqual(
                migration.delete_candidates(
                    plan,
                    confirmed_hash=plan["plan_hash"],
                    backup_dir=backups,
                    allow_delete=True,
                    adb=fake,
                ),
                {"deleted": 0},
            )
            self.assertTrue(Path(directory, "旧文件.m4a").exists())
            result = migration.delete_candidates(
                plan,
                confirmed_hash=plan["plan_hash"],
                backup_dir=backups,
                allow_delete=True,
                confirm_files=["旧文件.m4a"],
                adb=fake,
            )
            self.assertEqual(result, {"deleted": 1})
            self.assertFalse(Path(directory, "旧文件.m4a").exists())


if __name__ == "__main__":
    unittest.main()
