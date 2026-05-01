import shutil
from pathlib import Path
from PIL import Image

# Import the worker function
from jd2021_installer.ui.workers.pipeline_workers import _ensure_jdnext_albumcoach_texture_from_coach

def test_ensure_jdnext_albumcoach_multicoach(tmp_path: Path):
    """Test that multiple coaches are composited into a single albumcoach texture."""
    codename = "TestMap"
    tex_dir = tmp_path / "menuart" / "textures"
    tex_dir.mkdir(parents=True)
    
    # Create 3 dummy coach images: Red, Green, Blue
    # We'll make them 100x100 pixels, with the left half transparent and right half colored
    colors = [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)]
    
    for i, color in enumerate(colors, start=1):
        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        # Draw a colored rectangle in the center
        for x in range(25, 75):
            for y in range(25, 100): # Anchored to bottom
                img.putpixel((x, y), color)
        
        img.save(tex_dir / f"{codename}_coach_{i}.png")
        
    # Run the function
    result = _ensure_jdnext_albumcoach_texture_from_coach(tmp_path, codename)
    assert result is True, "Function should return True upon successful synthesis"
    
    # Verify the output file exists
    albumcoach_path = tex_dir / f"{codename}_cover_albumcoach.png"
    assert albumcoach_path.exists(), "cover_albumcoach.png was not generated"
    
    # Open the composite image and verify it has a mix of colors (all 3 coaches)
    composite = Image.open(albumcoach_path).convert("RGBA")
    
    # Assert dimensions are correct (compositing always produces 1024x1024)
    assert composite.size == (1024, 1024), "Composite image should be 1024x1024"
    
    # Check that pixels from the 3 coaches are present somewhere in the composite
    colors_found = set()
    for x in range(0, 1024, 4):
        for y in range(0, 1024, 4):
            pixel = composite.getpixel((x, y))
            if pixel in colors:
                colors_found.add(pixel)
                
    assert len(colors_found) == 3, f"Expected all 3 coach colors in composite, but found {len(colors_found)}"

def test_ensure_jdnext_albumcoach_singlecoach(tmp_path: Path):
    """Test that a single coach is simply copied."""
    codename = "TestMapSolo"
    tex_dir = tmp_path / "menuart" / "textures"
    tex_dir.mkdir(parents=True)
    
    img = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
    coach_path = tex_dir / f"{codename}_coach_1.png"
    img.save(coach_path)
    
    result = _ensure_jdnext_albumcoach_texture_from_coach(tmp_path, codename)
    assert result is True
    
    albumcoach_path = tex_dir / f"{codename}_cover_albumcoach.png"
    assert albumcoach_path.exists()
    
    # Since it's copied, it should be an exact duplicate
    orig_bytes = coach_path.read_bytes()
    new_bytes = albumcoach_path.read_bytes()
    assert orig_bytes == new_bytes, "For a single coach, the file should be a direct copy"
