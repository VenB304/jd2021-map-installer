import unittest
from pathlib import Path
import shutil
import tempfile
import os
from jd2021_installer.parsers.normalizer import _discover_media
from jd2021_installer.core.models import MapMedia
from jd2021_installer.installers.media_processor import process_menu_art
from jd2021_installer.installers.texture_decoder import decode_menuart_textures
from jd2021_installer.ui.workers.pipeline_workers import _install_menuart_companion_assets

from PIL import Image

class TestMenuArt(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.codename = "TestMap"

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _create_dummy_image(self, path):
        img = Image.new('RGBA', (1, 1), color=(255, 0, 0, 255))
        img.save(path, format='TGA')

    def test_cover_discovery_variants(self):
        # Case 1: Only generic cover exists
        self._create_dummy_image(self.test_dir / "TestMap_Cover_Generic.tga.ckd")
        media = _discover_media(self.test_dir, self.codename.lower())
        
        self.assertIsNotNone(media.cover_generic_path)
        self.assertIsNotNone(media.cover_online_path)

    def test_cover_discovery_both(self):
        # Case 2: Both exist
        self._create_dummy_image(self.test_dir / "TestMap_Cover_Generic.tga.ckd")
        self._create_dummy_image(self.test_dir / "TestMap_Cover_Online.tga.ckd")
        media = _discover_media(self.test_dir, self.codename.lower())
        
        self.assertEqual(media.cover_generic_path.name, "TestMap_Cover_Generic.tga.ckd")
        self.assertEqual(media.cover_online_path.name, "TestMap_Cover_Online.tga.ckd")

    def test_menu_art_healing(self):
        # Setup target directory structure
        target = self.test_dir / "target"
        tex_dir = target / "menuart" / "textures"
        tex_dir.mkdir(parents=True)
        
        # Only generic cover in target (decoded)
        self._create_dummy_image(tex_dir / "TestMap_cover_generic.tga")
        
        # Heal
        process_menu_art(target, "TestMap")
        
        # Verify online cover was created
        self.assertTrue((tex_dir / "TestMap_cover_online.tga").exists())

    def test_banner_synthesis_from_bkg(self):
        # Setup
        self._create_dummy_image(self.test_dir / "TestMap_map_bkg.png")
        
        # Discover
        media = _discover_media(self.test_dir, "testmap")
        
        # Verify map_bkg is discovered but banner_bkg stays optional/missing
        self.assertIsNone(media.banner_bkg_path)
        self.assertIsNotNone(media.map_bkg_path)
        self.assertEqual(media.map_bkg_path.name, "TestMap_map_bkg.png")

    def test_bundle_scoping_no_leakage(self):
        # Setup two maps
        map_a_dir = self.test_dir / "MapA"
        map_a_dir.mkdir()
        self._create_dummy_image(map_a_dir / "MapA_banner_bkg.png")
        
        map_b_dir = self.test_dir / "MapB"
        map_b_dir.mkdir()
        # Map B has NO banner
        
        # Discover Map B from the root (simulating bundle scan)
        media_b = _discover_media(self.test_dir, "mapb")
        
        # Verify Map B did NOT "steal" Map A's banner
        self.assertIsNone(media_b.banner_bkg_path)

    def test_discovery_ignores_actor_ckd_for_cover_assets(self):
        # Setup map tree with both actor and texture files using same cover token.
        actor_dir = self.test_dir / "menuart" / "actors"
        tex_dir = self.test_dir / "menuart" / "textures"
        actor_dir.mkdir(parents=True)
        tex_dir.mkdir(parents=True)

        (actor_dir / "TestMap_cover_generic.act.ckd").write_bytes(b"ACTOR")
        (tex_dir / "TestMap_cover_generic.tga.ckd").write_bytes(b"TEX")

        media = _discover_media(self.test_dir, "testmap")

        self.assertIsNotNone(media.cover_generic_path)
        self.assertEqual(media.cover_generic_path.name, "TestMap_cover_generic.tga.ckd")

    def test_media_processor_does_not_synthesize_banner(self):
        # Setup target directory structure
        target = self.test_dir / "target_proc"
        tex_dir = target / "menuart" / "textures"
        tex_dir.mkdir(parents=True)
        
        # Only map_bkg in target
        self._create_dummy_image(tex_dir / "TestMap_map_bkg.tga")
        
        # Heal
        process_menu_art(target, "TestMap")
        
        # Verify banner_bkg remains optional
        self.assertFalse((tex_dir / "TestMap_banner_bkg.tga").exists())

    def test_optional_assets_discovered_without_codename_prefix(self):
        """
        Test that optional assets (banner_bkg, map_bkg, cover_albumcoach, cover_albumbkg)
        are discovered even when they don't have codename prefix in filename.
        This is a regression test for V2 issue where optional .act files were missing.
        """
        # Setup: Create optional assets WITHOUT codename prefix (as some map makers do)
        self._create_dummy_image(self.test_dir / "albumcoach.tga")
        self._create_dummy_image(self.test_dir / "map_bkg.tga")
        self._create_dummy_image(self.test_dir / "banner_bkg.tga")
        self._create_dummy_image(self.test_dir / "albumbkg.tga")  # cover_albumbkg keyword
        
        # Also create required assets with codename prefix (as they should be)
        self._create_dummy_image(self.test_dir / "TestMap_cover_generic.tga")
        
        # Discover with explicit codename (simulating normal installation flow)
        media = _discover_media(self.test_dir, "testmap")
        
        # Verify required assets are found
        self.assertIsNotNone(media.cover_generic_path)
        
        # Verify optional assets are discovered even without codename prefix
        self.assertIsNotNone(media.cover_albumcoach_path, 
                           "Optional cover_albumcoach should be discovered without codename prefix")
        self.assertIsNotNone(media.map_bkg_path,
                           "Optional map_bkg should be discovered without codename prefix")
        self.assertIsNotNone(media.banner_bkg_path,
                           "Optional banner_bkg should be discovered without codename prefix")
        self.assertIsNotNone(media.cover_albumbkg_path,
                           "Optional cover_albumbkg should be discovered without codename prefix")

    def test_decode_menuart_counts_loose_png(self):
        src = self.test_dir / "menuart_src"
        dst = self.test_dir / "menuart_dst"
        src.mkdir(parents=True)
        dst.mkdir(parents=True)

        img = Image.new('RGBA', (8, 8), color=(0, 255, 0, 255))
        img.save(src / "TestMap_cover_phone.png", format='PNG')

        decoded = decode_menuart_textures(src, dst)

        self.assertEqual(decoded, 1)
        self.assertTrue((dst / "TestMap_cover_phone.png").exists())

    def test_install_menuart_companion_assets_converts_ckd_suffix(self):
        src = self.test_dir / "menuart_src"
        src.mkdir(parents=True)

        act_ckd = src / "TestMap_cover_generic.act.ckd"
        isc_ckd = src / "TestMap_menuart.isc.ckd"
        act_ckd.write_text("ACT_PAYLOAD", encoding="utf-8")
        isc_ckd.write_text("ISC_PAYLOAD", encoding="utf-8")

        target = self.test_dir / "target"
        copied = _install_menuart_companion_assets([src], target)

        self.assertEqual(copied, 2)
        self.assertTrue((target / "MenuArt" / "Actors" / "TestMap_cover_generic.act").exists())
        self.assertTrue((target / "MenuArt" / "TestMap_menuart.isc").exists())
    def test_install_menuart_companion_skips_binary_cooked_act(self):
        """Regression test: binary-cooked .act.ckd from IPK must not overwrite text .act."""
        src = self.test_dir / "menuart_src"
        src.mkdir(parents=True)

        # Simulate a binary-cooked .act.ckd (has null bytes like real IPK payloads)
        binary_act = src / "TestMap_coach_1.act.ckd"
        binary_act.write_bytes(
            b"\x00\x00\x00\x00\x3f\x80\x00\x00"
            b"tpl_materialgraphiccomponent2d.tpl\x00"
            b"enginedata/actortemplates/\x00"
        )

        target = self.test_dir / "target"
        copied = _install_menuart_companion_assets([src], target)

        # Binary payload should be skipped
        self.assertEqual(copied, 0)
        self.assertFalse(
            (target / "MenuArt" / "Actors" / "TestMap_coach_1.act").exists(),
            "Binary-cooked .act.ckd should NOT be installed as .act",
        )

    def test_install_menuart_companion_skips_existing_destination(self):
        """Existing text .act from game_writer must not be overwritten by CKD copy."""
        src = self.test_dir / "menuart_src"
        src.mkdir(parents=True)

        # Text payload that would normally be installable
        text_ckd = src / "TestMap_cover_generic.act.ckd"
        text_ckd.write_text("params = { replacement }", encoding="utf-8")

        target = self.test_dir / "target"
        actor_dir = target / "MenuArt" / "Actors"
        actor_dir.mkdir(parents=True)

        # Pre-existing .act from game_writer
        existing = actor_dir / "TestMap_cover_generic.act"
        existing.write_text("params = { original }", encoding="utf-8")

        copied = _install_menuart_companion_assets([src], target)

        self.assertEqual(copied, 0)
        # Original content must be preserved
        self.assertEqual(
            existing.read_text(encoding="utf-8"),
            "params = { original }",
        )

if __name__ == "__main__":
    unittest.main()
