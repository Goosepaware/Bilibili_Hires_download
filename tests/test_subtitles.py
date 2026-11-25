import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bili_audio import api, cli, subtitles
from bili_audio.http import BiliError
from bili_audio.models import Page


BV = "BV1GJ4m1g7Sk"


def player_track(url):
    return {"lan": "zh-CN", "lan_doc": "中文（中国）", "subtitle_url": url}


def response(*body):
    result = Mock(status_code=200)
    result.json.return_value = {"body": body}
    return result


class SubtitleTests(unittest.TestCase):
    def test_player_signs_each_part(self):
        tracks = [player_track("//aisubtitle.hdslb.com/a")]
        with patch.object(subtitles, "sign", return_value={"signed": 1}) as sign, \
                patch.object(subtitles, "api_get", return_value={"data": {"subtitle": {"subtitles": tracks}}}) as get:
            session = object()
            self.assertEqual(subtitles.get_tracks(session, BV, 19), tracks)
            sign.assert_called_once_with(session, {"bvid": BV, "cid": 19})
            get.assert_called_once_with(session, "/x/player/wbi/v2", {"signed": 1})

    def test_fetch_rejects_external_and_redirects(self):
        session = Mock()
        with self.assertRaises(BiliError):
            subtitles.fetch_cues(session, "https://hdslb.com.example.org/subtitle")
        session.get.assert_not_called()
        session.get.return_value.__enter__ = Mock(return_value=Mock(status_code=302))
        session.get.return_value.__exit__ = Mock(return_value=False)
        with self.assertRaises(BiliError):
            subtitles.fetch_cues(session, "//subtitle.bilibili.com/temporary")
        session.get.assert_called_once_with("https://subtitle.bilibili.com/temporary", timeout=15, allow_redirects=False)

    def test_cli_exports_same_language_tracks_and_compares_actual_text(self):
        first = {"from": 2.125, "to": 4, "content": "テスト"}
        second = {"from": 2, "to": 3, "content": "第一段"}
        third = {"from": 3, "to": 4, "content": "第二段"}
        outside = {"from": 5, "to": 6, "content": "额外文本"}
        a, b = "//aisubtitle.hdslb.com/one", "//aisubtitle.hdslb.com/two"
        video = SimpleNamespace(bvid=BV, title="测试", pages=[Page(1, 7, "分P一"), Page(2, 8, "分P二")])
        session = Mock()
        cdn = Mock()
        cdn.get.return_value.__enter__ = Mock(side_effect=[response(first), response(second, third, outside)])
        cdn.get.return_value.__exit__ = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(cli.auth, "current_session", return_value=(session, {"vip": False})), \
                patch.object(api, "get_video", return_value=video), \
                patch.object(subtitles, "get_tracks", return_value=[player_track(a), player_track(b)]) as get_tracks, \
                patch.object(subtitles, "new_session") as new_session:
            new_session.return_value.__enter__.return_value = cdn
            self.assertEqual(cli.main(["subtitles", BV, "-p", "2", "--compare", "-o", temp]), 0)
            get_tracks.assert_called_once_with(session, BV, 8)
            new_session.assert_called_once_with()
            files = sorted((Path(temp) / BV).glob("*.srt"))
            self.assertEqual(len(files), 2)
            self.assertIn("00:00:02,125 --> 00:00:04,000\nテスト", files[0].read_text(encoding="utf-8"))
            report = json.loads(next((Path(temp) / BV).glob("*.compare.json")).read_text(encoding="utf-8"))
            compared = report["comparisons"][0]
            self.assertEqual([m["text"] for m in compared["rows"][0]["matches"]], ["第一段", "第二段"])
            self.assertEqual(compared["unmatched"][0]["text"], "额外文本")

    def test_audio_command_only_downloads_subtitles_once_per_part(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(cli.auth, "current_session", return_value=(object(), None)) as current, \
                patch.object(cli, "find_ffmpeg"), patch.object(cli, "load_config", return_value={}), \
                patch.object(cli, "save_config"), patch.object(cli, "process_url") as process, \
                patch.object(subtitles, "export_page", return_value=0) as export:
            stem = Path(temp) / "标题"
            recording = SimpleNamespace(page=Page(2, 9, "测试"), video=SimpleNamespace(bvid=BV))
            process.return_value = [SimpleNamespace(**vars(recording), path=str(stem) + ext) for ext in (".flac", ".eac3")]
            self.assertEqual(cli.main(["dl", BV, "-o", temp]), 0)
            export.assert_not_called()
            self.assertEqual(cli.main(["dl", BV, "-o", temp, "--subtitles"]), 0)
            export.assert_called_once_with(current.return_value[0], BV, 9, stem)


if __name__ == "__main__":
    unittest.main()
