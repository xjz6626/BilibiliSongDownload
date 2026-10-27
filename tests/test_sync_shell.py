import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


FISH = shutil.which("fish")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(FISH, "fish is required for shell workflow tests")
class SyncShellTests(unittest.TestCase):
    def test_partial_adb_failure_only_removes_successful_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            music_dir = root / "Music_Done"
            music_dir.mkdir()
            good = music_dir / "good [BV1Ab411c7DE].m4a"
            failed = music_dir / "fail [BV1Xx411c7YY].m4a"
            good.touch()
            failed.touch()
            shutil.copy2(PROJECT_ROOT / "sync-nusic.fish.example", root / "sync-nusic.fish")

            wrapper = r'''
function adb
    if test "$argv[1]" = devices
        printf 'List of devices attached\nserial-1\tdevice\n'
        return 0
    end
    set -l action_index 1
    if test "$argv[1]" = -s
        set action_index 3
    end
    if test "$argv[$action_index]" = shell
        return 0
    end
    if test "$argv[$action_index]" = push
        if string match -q '*fail*' -- $argv
            return 1
        end
        return 0
    end
    return 1
end
function yt-dlp
    return 0
end
function python3
    return 0
end
source ./sync-nusic.fish
'''
            result = subprocess.run(
                [FISH, "-c", wrapper],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertFalse(good.exists(), result.stdout)
            self.assertTrue(failed.exists(), result.stdout)
            self.assertFalse((music_dir / ".sync.lock").exists(), result.stdout)

    def test_download_error_syncs_successes_but_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            music_dir = root / "Music_Done"
            music_dir.mkdir()
            downloaded = music_dir / "downloaded [BV1Ab411c7DE].m4a"
            downloaded.touch()
            shutil.copy2(PROJECT_ROOT / "sync-nusic.fish.example", root / "sync-nusic.fish")

            wrapper = r'''
function adb
    if test "$argv[1]" = devices
        printf 'List of devices attached\nserial-1\tdevice\n'
        return 0
    end
    return 0
end
function yt-dlp
    echo 'ERROR: injected download failure'
    return 0
end
function python3
    return 0
end
source ./sync-nusic.fish
'''
            result = subprocess.run(
                [FISH, "-c", wrapper],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertFalse(downloaded.exists(), result.stdout)
            self.assertIn("仍有下载失败项", result.stdout)
            self.assertFalse((music_dir / ".sync.lock").exists(), result.stdout)

    def test_festival_page_error_is_resolved_and_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            music_dir = root / "Music_Done"
            music_dir.mkdir()
            downloaded = music_dir / "festival [BV1G4346fEFU].m4a"
            downloaded.touch()
            shutil.copy2(PROJECT_ROOT / "sync-nusic.fish.example", root / "sync-nusic.fish")

            wrapper = r'''
set -g yt_dlp_calls 0
function adb
    if test "$argv[1]" = devices
        printf 'List of devices attached\nserial-1\tdevice\n'
    end
    return 0
end
function yt-dlp
    set -g yt_dlp_calls (math $yt_dlp_calls + 1)
    if test $yt_dlp_calls -eq 1
        echo 'ERROR: [BiliBili] 1G4346fEFU: Unable to extract initial state; please report this issue'
    else
        string join \n -- $argv > captured-retry-args.txt
    end
    return 0
end
function python3
    if string match -q '*bilibili_festival_retry.py' -- $argv[1]
        echo 'https://www.bilibili.com/festival/test-event?bvid=BV1G4346fEFU'
    end
    return 0
end
source ./sync-nusic.fish
'''
            result = subprocess.run(
                [FISH, "-c", wrapper],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse(downloaded.exists(), result.stdout)
            retry_args = (root / "captured-retry-args.txt").read_text()
            self.assertIn("https://www.bilibili.com/festival/test-event?bvid=BV1G4346fEFU", retry_args)
            self.assertIn("--no-playlist", retry_args)
            self.assertIn("使用官方活动地址重试", result.stdout)

    def test_playlist_argument_is_normalized_and_forwarded(self):
        cases = [
            ("", "1:100"),
            ("100", "1:100"),
            ("51:100", "51:100"),
            ("1,3,8", "1,3,8"),
        ]
        for argument, expected in cases:
            with self.subTest(argument=argument), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copy2(PROJECT_ROOT / "sync-nusic.fish.example", root / "sync-nusic.fish")
                source_command = f"source ./sync-nusic.fish {argument}".rstrip()
                wrapper = rf'''
function adb
    if test "$argv[1]" = devices
        printf 'List of devices attached\nserial-1\tdevice\n'
    end
    return 0
end
function yt-dlp
    for index in (seq (count $argv))
        if test "$argv[$index]" = --playlist-items
            set -l value_index (math $index + 1)
            printf '%s\n' "$argv[$value_index]" > captured-items.txt
        end
    end
    return 0
end
function python3
    return 0
end
{source_command}
'''
                result = subprocess.run(
                    [FISH, "-c", wrapper],
                    cwd=root,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual((root / "captured-items.txt").read_text().strip(), expected)
                self.assertIn(f"本次范围: {expected}", result.stdout)

    def test_invalid_playlist_argument_exits_before_workflow(self):
        result = subprocess.run(
            [FISH, os.fspath(PROJECT_ROOT / "sync-nusic.fish.example"), "not-a-range"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("无效的收藏夹范围", result.stdout)

    def test_example_uses_bvid_thumbnail_conversion_and_info_json(self):
        script = (PROJECT_ROOT / "sync-nusic.fish.example").read_text(encoding="utf-8")
        self.assertIn("%(title)s [%(id)s].%(ext)s", script)
        self.assertIn("--convert-thumbnails jpg", script)
        self.assertIn("--write-info-json", script)
        self.assertIn("bilibili_festival_retry.py", script)
        self.assertNotIn('rm -rf "$MUSIC_DIR"/*', script)


if __name__ == "__main__":
    unittest.main()
