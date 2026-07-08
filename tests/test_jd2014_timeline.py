import pytest
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from jd2021_installer.parsers.binary_ckd import parse_binary_ckd
from jd2021_installer.core.models import (
    DanceTape, MotionClip, PictogramClip, GoldEffectClip, KaraokeTape, KaraokeClip
)
from jd2021_installer.parsers.normalizer import _extract_dance_tape, _extract_karaoke_tape
from jd2021_installer.installers.tape_converter import auto_convert_tapes

# Path to the extracted JD2014 game on the user's system
JD2014_ROOT = Path(r"d:\jd360\Just Dance 2014 (USA)\Just Dance 2014 (USA)")
JUSTDANCE_TIMELINE_DIR = JD2014_ROOT / "Bundle_0_X360" / "cache" / "itf_cooked" / "x360" / "world" / "jd5" / "justdance" / "timeline"
WILD_TIMELINE_DIR = JD2014_ROOT / "Bundle_0_X360" / "cache" / "itf_cooked" / "x360" / "world" / "jd5" / "wild" / "timeline"

has_jd2014 = JUSTDANCE_TIMELINE_DIR.exists() and WILD_TIMELINE_DIR.exists()

@pytest.mark.skipif(not has_jd2014, reason="Just Dance 2014 extracted game files not found on this system")
def test_jd2014_justdance_parsing():
    tpl_path = JUSTDANCE_TIMELINE_DIR / "timeline.tpl.ckd"
    assert tpl_path.is_file()
    
    data = tpl_path.read_bytes()
    res = parse_binary_ckd(data, tpl_path.name)
    
    assert isinstance(res, DanceTape)
    assert res.map_name == "JustDance"
    
    motion_clips = [c for c in res.clips if isinstance(c, MotionClip)]
    picto_clips = [c for c in res.clips if isinstance(c, PictogramClip)]
    gold_clips = [c for c in res.clips if isinstance(c, GoldEffectClip)]
    
    assert len(motion_clips) == 251
    assert len(picto_clips) == 143
    assert len(gold_clips) == 6
    assert any(c.gold_move == 1 for c in motion_clips)

@pytest.mark.skipif(not has_jd2014, reason="Just Dance 2014 extracted game files not found on this system")
def test_jd2014_justdance_karaoke_parsing():
    tpl_path = JUSTDANCE_TIMELINE_DIR / "timeline.tpl.ckd"
    assert tpl_path.is_file()
    
    data = tpl_path.read_bytes()
    res = parse_binary_ckd(data, tpl_path.name, force_karaoke=True)
    
    assert isinstance(res, KaraokeTape)
    assert res.map_name == "JustDance"
    assert len(res.clips) == 508
    
    first_clip = res.clips[0]
    assert isinstance(first_clip, KaraokeClip)
    assert first_clip.lyrics == "RedOne "
    assert first_clip.start_time == 19034
    assert first_clip.is_end_of_line == 0

@pytest.mark.skipif(not has_jd2014, reason="Just Dance 2014 extracted game files not found on this system")
def test_jd2014_wild_parsing():
    tpl_path = WILD_TIMELINE_DIR / "timeline.tpl.ckd"
    assert tpl_path.is_file()
    
    data = tpl_path.read_bytes()
    res = parse_binary_ckd(data, tpl_path.name)
    
    assert isinstance(res, DanceTape)
    assert res.map_name == "Wild"
    
    motion_clips = [c for c in res.clips if isinstance(c, MotionClip)]
    picto_clips = [c for c in res.clips if isinstance(c, PictogramClip)]
    
    assert len(motion_clips) == 128
    assert len(picto_clips) == 72

@pytest.mark.skipif(not has_jd2014, reason="Just Dance 2014 extracted game files not found on this system")
def test_jd2014_wild_karaoke_parsing():
    tpl_path = WILD_TIMELINE_DIR / "timeline.tpl.ckd"
    assert tpl_path.is_file()
    
    data = tpl_path.read_bytes()
    res = parse_binary_ckd(data, tpl_path.name, force_karaoke=True)
    
    assert isinstance(res, KaraokeTape)
    assert res.map_name == "Wild"
    assert len(res.clips) == 520
    
    first_clip = res.clips[0]
    assert first_clip.lyrics == "Jessie "
    assert first_clip.start_time == 6868
    assert first_clip.track_id == 3

@pytest.mark.skipif(not has_jd2014, reason="Just Dance 2014 extracted game files not found on this system")
def test_normalizer_extract_dance_tape_fallback():
    res = _extract_dance_tape(str(JUSTDANCE_TIMELINE_DIR.parent), "justdance")
    assert isinstance(res, DanceTape)
    assert res.map_name == "JustDance"
    
    motion_clips = [c for c in res.clips if isinstance(c, MotionClip)]
    assert len(motion_clips) == 251

@pytest.mark.skipif(not has_jd2014, reason="Just Dance 2014 extracted game files not found on this system")
def test_normalizer_extract_karaoke_tape_fallback():
    res = _extract_karaoke_tape(str(JUSTDANCE_TIMELINE_DIR.parent), "justdance")
    assert isinstance(res, KaraokeTape)
    assert res.map_name == "JustDance"
    assert len(res.clips) == 508

@pytest.mark.skipif(not has_jd2014, reason="Just Dance 2014 extracted game files not found on this system")
def test_tape_converter_auto_convert_tapes(tmp_path):
    converted = auto_convert_tapes(JUSTDANCE_TIMELINE_DIR.parent, tmp_path, "justdance")
    assert converted >= 2 # at least dance and karaoke
    
    output_dance = tmp_path / "timeline" / "justdance_TML_Dance.dtape"
    output_karaoke = tmp_path / "timeline" / "justdance_TML_Karaoke.ktape"
    
    assert output_dance.is_file()
    assert output_karaoke.is_file()
    
    dance_lua = output_dance.read_text(encoding="utf-8")
    assert '"world/maps/justdance/timeline/moves/justdance_martialwalkdown.msm"' in dance_lua
    
    karaoke_lua = output_karaoke.read_text(encoding="utf-8")
    assert 'NAME = "KaraokeClip"' in karaoke_lua
    assert '"RedOne "' in karaoke_lua
