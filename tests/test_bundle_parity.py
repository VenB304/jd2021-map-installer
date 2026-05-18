import unittest
import os
import shutil
import tempfile
from unittest.mock import patch
from pathlib import Path
try:
    from PIL import Image
except Exception:
    Image = None
from jd2021_installer.extractors.archive_ipk import _detect_maps_in_dir
from jd2021_installer.parsers.normalizer import _discover_media, _find_ckd_files
from jd2021_installer.core.models import (
    MapMedia,
    MapSync,
    MusicTrackStructure,
    NormalizedMapData,
    SongDescription,
)
from jd2021_installer.core.config import AppConfig
from jd2021_installer.ui.workers.pipeline_workers import (
    _apply_jdnext_bottom_alpha_fade_if_needed,
    install_map_to_game,
)

class TestBundleParity(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_detect_maps_standard(self):
        # Create standard layout: world/maps/<codename>
        (self.test_dir / "world" / "maps" / "MapA").mkdir(parents=True)
        (self.test_dir / "world" / "maps" / "MapB").mkdir(parents=True)
        (self.test_dir / "common").mkdir()
        
        maps = _detect_maps_in_dir(self.test_dir)
        self.assertEqual(maps, ["MapA", "MapB"])

    def test_detect_maps_legacy(self):
        # Create legacy layout: world/jd2015/<codename>
        (self.test_dir / "world" / "jd2015" / "MapC").mkdir(parents=True)
        (self.test_dir / "world" / "jd2015" / "MapD").mkdir(parents=True)
        (self.test_dir / "enginedata").mkdir()
        
        maps = _detect_maps_in_dir(self.test_dir)
        self.assertEqual(maps, ["MapC", "MapD"])

    def test_video_scoping_bundle(self):
        # Mock extracted bundle with multiple videos
        map_a_dir = self.test_dir / "world" / "maps" / "MapA"
        map_a_dir.mkdir(parents=True)
        vid_a = map_a_dir / "videos" / "video.webm"
        vid_a.parent.mkdir(parents=True)
        vid_a.touch()
        
        map_b_dir = self.test_dir / "world" / "maps" / "MapB"
        map_b_dir.mkdir(parents=True)
        vid_b = map_b_dir / "videos" / "MapB_ULTRA.webm"
        vid_b.parent.mkdir(parents=True)
        vid_b.touch()
        
        # Discover for MapA
        media = _discover_media(self.test_dir, codename="MapA")
        self.assertEqual(media.video_path, vid_a)
        
        # Discover for MapB
        media = _discover_media(self.test_dir, codename="MapB")
        self.assertEqual(media.video_path, vid_b)

    def test_find_ckd_bundle_scoping(self):
        (self.test_dir / "world" / "maps" / "MapA").mkdir(parents=True)
        mt_a = self.test_dir / "world" / "maps" / "MapA" / "musictrack.tpl.ckd"
        mt_a.touch()
        
        (self.test_dir / "world" / "maps" / "MapB").mkdir(parents=True)
        mt_b = self.test_dir / "world" / "maps" / "MapB" / "musictrack.tpl.ckd"
        mt_b.touch()
        
        res_a = _find_ckd_files(str(self.test_dir), "*musictrack*.tpl.ckd", codename="MapA")
        self.assertEqual(len(res_a), 1)
        self.assertIn("MapA", res_a[0])
        
        res_b = _find_ckd_files(str(self.test_dir), "*musictrack*.tpl.ckd", codename="MapB")
        self.assertEqual(len(res_b), 1)
        self.assertIn("MapB", res_b[0])

    def test_search_root_discovers_bundle_menuart_pictos_moves(self):
        # map subtree used for normalization
        map_subtree = self.test_dir / "world" / "maps" / "MapA"
        map_subtree.mkdir(parents=True)

        # assets in cache layout outside world/maps/<codename>
        cache_root = self.test_dir / "cache" / "itf_cooked" / "x360" / "world" / "maps" / "MapA"
        menuart = cache_root / "menuart" / "textures"
        pictos = cache_root / "timeline" / "pictos"
        moves = cache_root / "timeline" / "moves"
        menuart.mkdir(parents=True)
        pictos.mkdir(parents=True)
        moves.mkdir(parents=True)

        cover = menuart / "MapA_cover_generic.tga.ckd"
        coach = menuart / "MapA_coach_1.tga.ckd"
        cover.touch()
        coach.touch()
        (pictos / "MapA_picto_001.png.ckd").touch()
        (moves / "MapA_move_001.gesture").touch()

        media = _discover_media(map_subtree, codename="MapA", search_roots=[self.test_dir])

        self.assertEqual(media.cover_generic_path, cover)
        self.assertIn(coach, media.coach_images)
        self.assertEqual(media.pictogram_dir, pictos)
        self.assertEqual(media.moves_dir, moves)

    def test_menuart_candidates_inside_pictos_are_copied_to_menuart_textures(self):
        source_dir = self.test_dir / "source"
        pictos_dir = source_dir / "timeline" / "pictos"
        pictos_dir.mkdir(parents=True)
        art_file = pictos_dir / "MapA_cover_generic.png"
        art_file.write_bytes(b"png")

        game_root = self.test_dir / "game"
        game_root.mkdir(parents=True)

        map_data = NormalizedMapData(
            codename="MapA",
            song_desc=SongDescription(map_name="MapA", title="MapA", artist="Artist"),
            music_track=MusicTrackStructure(markers=[0, 2400, 4800], start_beat=0, end_beat=2),
            media=MapMedia(pictogram_dir=pictos_dir),
            sync=MapSync(audio_ms=0.0, video_ms=0.0),
            source_dir=source_dir,
        )
        config = AppConfig(game_directory=game_root, cache_directory=self.test_dir / "cache")

        with patch("jd2021_installer.ui.workers.pipeline_workers.pre_install_cleanup"), \
             patch("jd2021_installer.ui.workers.pipeline_workers.reprocess_audio"), \
             patch("jd2021_installer.installers.tape_converter.auto_convert_tapes"), \
             patch("jd2021_installer.installers.ambient_processor.process_ambient_directory"), \
             patch("jd2021_installer.installers.texture_decoder.decode_menuart_textures"), \
             patch("jd2021_installer.installers.texture_decoder.decode_pictograms"), \
             patch("jd2021_installer.installers.media_processor.process_menu_art"), \
             patch("jd2021_installer.installers.sku_scene.register_map"), \
             patch("jd2021_installer.installers.autodance_processor.process_stape_file"):
            install_map_to_game(map_data, game_root, config)

        copied = game_root / "data" / "world" / "maps" / "MapA" / "menuart" / "textures" / "MapA_cover_generic.png"
        self.assertTrue(copied.exists())

    def test_install_preserves_main_video_and_only_processes_preview_video(self):
        source_dir = self.test_dir / "source"
        source_dir.mkdir(parents=True)

        gameplay_video = source_dir / "MapA_ULTRA.webm"
        preview_video = source_dir / "MapA_MapPreview.webm"
        gameplay_video.write_bytes(b"gameplay-video-bytes")
        preview_video.write_bytes(b"preview-video-bytes")

        game_root = self.test_dir / "game"
        game_root.mkdir(parents=True)

        map_data = NormalizedMapData(
            codename="MapA",
            song_desc=SongDescription(map_name="MapA", title="MapA", artist="Artist"),
            music_track=MusicTrackStructure(markers=[0, 2400, 4800], start_beat=0, end_beat=2),
            media=MapMedia(video_path=gameplay_video, map_preview_video=preview_video),
            sync=MapSync(audio_ms=0.0, video_ms=0.0),
            source_dir=source_dir,
        )
        config = AppConfig(game_directory=game_root, cache_directory=self.test_dir / "cache")

        with patch("jd2021_installer.ui.workers.pipeline_workers.pre_install_cleanup"), \
             patch("jd2021_installer.ui.workers.pipeline_workers.reprocess_audio"), \
             patch("jd2021_installer.installers.tape_converter.auto_convert_tapes"), \
             patch("jd2021_installer.installers.ambient_processor.process_ambient_directory"), \
             patch("jd2021_installer.installers.texture_decoder.decode_menuart_textures"), \
             patch("jd2021_installer.installers.texture_decoder.decode_pictograms"), \
             patch("jd2021_installer.installers.media_processor.process_menu_art"), \
             patch("jd2021_installer.installers.sku_scene.register_map"), \
             patch("jd2021_installer.installers.autodance_processor.process_stape_file"), \
             patch("jd2021_installer.installers.media_processor.copy_video") as mock_copy_video:
            install_map_to_game(map_data, game_root, config)

        expected_main_dst = game_root / "data" / "world" / "maps" / "MapA" / "videoscoach" / "MapA.webm"
        expected_preview_dst = game_root / "data" / "world" / "maps" / "MapA" / "videoscoach" / "MapA_MapPreview.webm"

        mock_copy_video.assert_any_call(gameplay_video, expected_main_dst, config=config)
        mock_copy_video.assert_any_call(preview_video, expected_preview_dst, config=config)

    def test_install_does_not_apply_jdnext_boost_in_fetch_mode(self):
        source_dir = self.test_dir / "source"
        source_dir.mkdir(parents=True)

        game_root = self.test_dir / "game"
        game_root.mkdir(parents=True)

        map_data = NormalizedMapData(
            codename="MapA",
            song_desc=SongDescription(map_name="MapA", title="MapA", artist="Artist"),
            music_track=MusicTrackStructure(markers=[0, 2400, 4800], start_beat=0, end_beat=2),
            media=MapMedia(),
            sync=MapSync(audio_ms=0.0, video_ms=0.0),
            source_dir=source_dir,
        )
        config = AppConfig(game_directory=game_root, cache_directory=self.test_dir / "cache")

        def _fake_reprocess(_map_data, target_dir, _a_offset=0.0, _config=None):
            audio_dir = target_dir / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            (audio_dir / "MapA.wav").write_bytes(b"wav")

        with patch("jd2021_installer.ui.workers.pipeline_workers.pre_install_cleanup"), \
             patch("jd2021_installer.ui.workers.pipeline_workers.reprocess_audio", side_effect=_fake_reprocess), \
             patch("jd2021_installer.installers.tape_converter.auto_convert_tapes"), \
             patch("jd2021_installer.installers.ambient_processor.process_ambient_directory"), \
             patch("jd2021_installer.installers.texture_decoder.decode_menuart_textures"), \
             patch("jd2021_installer.installers.texture_decoder.decode_pictograms"), \
             patch("jd2021_installer.installers.media_processor.process_menu_art"), \
             patch("jd2021_installer.installers.sku_scene.register_map"), \
             patch("jd2021_installer.installers.autodance_processor.process_stape_file"), \
             patch("jd2021_installer.installers.media_processor.apply_audio_gain") as mock_apply_gain:
            install_map_to_game(map_data, game_root, config, source_mode="fetch_jdnext")

        mock_apply_gain.assert_not_called()

    def test_install_creates_optional_albumcoach_act_from_late_texture_copy(self):
        source_dir = self.test_dir / "source"
        pictos_dir = source_dir / "timeline" / "pictos"
        pictos_dir.mkdir(parents=True)
        (pictos_dir / "cover_albumcoach.png").write_bytes(b"png")

        game_root = self.test_dir / "game"
        game_root.mkdir(parents=True)

        map_data = NormalizedMapData(
            codename="MapA",
            song_desc=SongDescription(map_name="MapA", title="MapA", artist="Artist"),
            music_track=MusicTrackStructure(markers=[0, 2400, 4800], start_beat=0, end_beat=2),
            media=MapMedia(pictogram_dir=pictos_dir),
            sync=MapSync(audio_ms=0.0, video_ms=0.0),
            source_dir=source_dir,
        )
        config = AppConfig(game_directory=game_root, cache_directory=self.test_dir / "cache")

        def _fake_reprocess(_map_data, target_dir, _a_offset=0.0, _config=None):
            actors_dir = target_dir / "MenuArt" / "Actors"
            actors_dir.mkdir(parents=True, exist_ok=True)
            # Simulate initial game writer output without optional actor files.
            (actors_dir / "MapA_cover_generic.act").write_text("generic", encoding="utf-8")
            (actors_dir / "MapA_cover_online.act").write_text("online", encoding="utf-8")

        with patch("jd2021_installer.ui.workers.pipeline_workers.pre_install_cleanup"), \
             patch("jd2021_installer.ui.workers.pipeline_workers.reprocess_audio", side_effect=_fake_reprocess), \
             patch("jd2021_installer.installers.tape_converter.auto_convert_tapes"), \
             patch("jd2021_installer.installers.ambient_processor.process_ambient_directory"), \
             patch("jd2021_installer.installers.texture_decoder.decode_menuart_textures"), \
             patch("jd2021_installer.installers.texture_decoder.decode_pictograms"), \
             patch("jd2021_installer.installers.media_processor.process_menu_art"), \
             patch("jd2021_installer.installers.sku_scene.register_map"), \
             patch("jd2021_installer.installers.autodance_processor.process_stape_file"):
            install_map_to_game(map_data, game_root, config)

        expected_act = game_root / "data" / "world" / "maps" / "MapA" / "MenuArt" / "Actors" / "MapA_cover_albumcoach.act"
        self.assertTrue(expected_act.exists())

    def test_install_synthesizes_jdnext_albumcoach_from_coach1(self):
        source_dir = self.test_dir / "source"
        source_dir.mkdir(parents=True)
        coach_1 = source_dir / "MapA_coach_1.png"
        coach_1.write_bytes(b"png")

        game_root = self.test_dir / "game"
        game_root.mkdir(parents=True)

        map_data = NormalizedMapData(
            codename="MapA",
            song_desc=SongDescription(map_name="MapA", title="MapA", artist="Artist"),
            music_track=MusicTrackStructure(markers=[0, 2400, 4800], start_beat=0, end_beat=2),
            media=MapMedia(coach_images=[coach_1]),
            sync=MapSync(audio_ms=0.0, video_ms=0.0),
            source_dir=source_dir,
        )
        config = AppConfig(game_directory=game_root, cache_directory=self.test_dir / "cache")

        def _fake_reprocess(_map_data, target_dir, _a_offset=0.0, _config=None):
            actors_dir = target_dir / "MenuArt" / "Actors"
            actors_dir.mkdir(parents=True, exist_ok=True)
            # Simulate initial game writer output without optional actor files.
            (actors_dir / "MapA_cover_generic.act").write_text("generic", encoding="utf-8")
            (actors_dir / "MapA_cover_online.act").write_text("online", encoding="utf-8")

        with patch("jd2021_installer.ui.workers.pipeline_workers.pre_install_cleanup"), \
             patch("jd2021_installer.ui.workers.pipeline_workers.reprocess_audio", side_effect=_fake_reprocess), \
             patch("jd2021_installer.installers.tape_converter.auto_convert_tapes"), \
             patch("jd2021_installer.installers.ambient_processor.process_ambient_directory"), \
             patch("jd2021_installer.installers.texture_decoder.decode_menuart_textures"), \
             patch("jd2021_installer.installers.texture_decoder.decode_pictograms"), \
             patch("jd2021_installer.installers.media_processor.process_menu_art"), \
             patch("jd2021_installer.installers.sku_scene.register_map"), \
             patch("jd2021_installer.installers.autodance_processor.process_stape_file"):
            install_map_to_game(map_data, game_root, config, source_mode="fetch_jdnext")

        texture_dir = game_root / "data" / "world" / "maps" / "MapA" / "menuart" / "textures"
        expected_texture = texture_dir / "MapA_cover_albumcoach.png"
        expected_act = game_root / "data" / "world" / "maps" / "MapA" / "MenuArt" / "Actors" / "MapA_cover_albumcoach.act"
        self.assertTrue(expected_texture.exists())
        self.assertTrue(expected_act.exists())

    @unittest.skipIf(Image is None, "Pillow not available")
    def test_jdnext_bottom_fade_applies_when_alpha_is_fully_opaque(self):
        texture_dir = self.test_dir / "game" / "data" / "world" / "maps" / "MapA" / "menuart" / "textures"
        texture_dir.mkdir(parents=True)
        coach_path = texture_dir / "MapA_coach_1.png"

        img = Image.new("RGBA", (16, 64), (255, 255, 255, 255))
        img.save(coach_path)

        updated = _apply_jdnext_bottom_alpha_fade_if_needed(
            self.test_dir / "game" / "data" / "world" / "maps" / "MapA",
            "MapA",
        )
        self.assertEqual(updated, 1)

        with Image.open(coach_path) as out:
            alpha = out.convert("RGBA").getchannel("A")
            top_alpha = alpha.getpixel((8, 8))
            bottom_alpha = alpha.getpixel((8, 63))

        self.assertGreaterEqual(top_alpha, 240)
        self.assertLessEqual(bottom_alpha, 8)

    @unittest.skipIf(Image is None, "Pillow not available")
    def test_jdnext_bottom_fade_skips_when_texture_already_transparent(self):
        texture_dir = self.test_dir / "game" / "data" / "world" / "maps" / "MapA" / "menuart" / "textures"
        texture_dir.mkdir(parents=True)
        coach_path = texture_dir / "MapA_coach_1.png"

        width, height = 16, 64
        img = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        px = img.load()
        fade_start = int(height * 0.70)
        den = max(1, (height - 1) - fade_start)
        for y in range(fade_start, height):
            fade = ((height - 1) - y) / den
            a = int(round(255 * (fade ** 1.35)))
            for x in range(width):
                px[x, y] = (255, 255, 255, a)
        img.save(coach_path)

        with Image.open(coach_path) as before_img:
            before_bottom_alpha = before_img.convert("RGBA").getchannel("A").getpixel((8, 63))

        updated = _apply_jdnext_bottom_alpha_fade_if_needed(
            self.test_dir / "game" / "data" / "world" / "maps" / "MapA",
            "MapA",
        )
        self.assertEqual(updated, 0)

        with Image.open(coach_path) as after_img:
            after_bottom_alpha = after_img.convert("RGBA").getchannel("A").getpixel((8, 63))

        self.assertEqual(before_bottom_alpha, after_bottom_alpha)

if __name__ == "__main__":
    unittest.main()
