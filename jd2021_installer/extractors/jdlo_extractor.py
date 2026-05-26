import logging
import os
import shutil
import zipfile
from pathlib import Path
from typing import Optional

from jd2021_installer.core.config import AppConfig
from jd2021_installer.core.jdlo_client import JDLOClient
from jd2021_installer.extractors.base import BaseExtractor

logger = logging.getLogger("jd2021.extractors.jdlo")

class JDLOExtractorError(Exception):
    pass

class JDLOOfflineExtractor(BaseExtractor):
    def __init__(self, asset_html: str, config: AppConfig):
        self._asset_html = Path(asset_html)
        self._config = config
        self._warnings: list[str] = []
        
        # Determine codename from directory
        self._codename = self._asset_html.parent.name
        self._download_dir = self._asset_html.parent

    def get_codename(self) -> Optional[str]:
        return self._codename

    def get_source_dir(self) -> Optional[Path]:
        return self._download_dir

    def get_warnings(self) -> list[str]:
        return self._warnings

    def extract(self, output_dir: Path) -> Path:
        if not self._download_dir.exists():
            raise JDLOExtractorError(f"Offline download directory not found: {self._download_dir}")
            
        extract_dir = output_dir / self._codename
        extract_dir.mkdir(parents=True, exist_ok=True)
        
        pkg_dest = self._download_dir / f"{self._codename}_MAIN_SCENE.zip"
        if not pkg_dest.exists():
            zips = list(self._download_dir.glob("*_MAIN_SCENE.zip"))
            if zips:
                pkg_dest = zips[0]
            else:
                raise JDLOExtractorError(f"Missing MAIN_SCENE zip in {self._download_dir.name}")
                
        logger.info(f"Extracting {pkg_dest.name}...")
        try:
            with zipfile.ZipFile(pkg_dest, "r") as z:
                z.extractall(extract_dir)
        except Exception as e:
            raise JDLOExtractorError(f"Failed to extract ZIP package: {e}")
            
        from jd2021_installer.extractors.archive_ipk import ArchiveIPKExtractor
        for ipk_file in extract_dir.rglob("*.ipk"):
            logger.info(f"Extracting bundled IPK {ipk_file.name}...")
            try:
                ipk_ext = ArchiveIPKExtractor(ipk_file, desired_codename=self._codename)
                ipk_ext.extract(extract_dir)
                ipk_file.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Failed to extract bundled IPK {ipk_file.name}: {e}")
                
        for f in os.listdir(self._download_dir):
            if f.endswith(".zip"):
                continue
            src_file = self._download_dir / f
            dst_file = extract_dir / f
            if src_file.is_file() and not dst_file.exists():
                shutil.copy2(src_file, dst_file)
                
        return extract_dir


class JDLOExtractor(BaseExtractor):
    def __init__(self, codenames: list[str], config: AppConfig):
        self._codenames = [c.strip() for c in codenames if c.strip()]
        self._config = config
        self._client = JDLOClient(config)
        self._warnings: list[str] = []
        self._download_dir = None

    def get_codename(self) -> Optional[str]:
        return self._codenames[0] if self._codenames else None

    def get_source_dir(self) -> Optional[Path]:
        return self._download_dir

    def get_warnings(self) -> list[str]:
        return self._warnings

    def _download_file(self, url: str, dest: Path) -> None:
        """Download a file streaming."""
        logger.info(f"Downloading {dest.name}...")
        
        try:
            if url.startswith("http://") or url.startswith("https://"):
                resp = self._client.download_request(url, stream=True)
            else:
                resp = self._client._make_request("GET", url.replace(self._client.BASE_URL, ""), {"User-Agent": self._client.USER_AGENT_DOWNLOAD}, stream=True)
        except Exception:
            # Fallback if the URL is not a JDLO API url
            resp = self._client.download_request(url, stream=True)

        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                if chunk:
                    f.write(chunk)

    def extract(self, output_dir: Path) -> Path:
        if not self._codenames:
            raise JDLOExtractorError("No codenames provided for JDLO Fetch.")
        
        codename = self._codenames[0]
        
        # Determine directories
        self._download_dir = self._config.download_root / "jdlo" / codename
        self._download_dir.mkdir(parents=True, exist_ok=True)
        
        extract_dir = output_dir / codename
        extract_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Fetch metadata
        try:
            songs_db = self._client.fetch_songs_db()
            packages_db = self._client.fetch_sku_packages()
        except Exception as e:
            raise JDLOExtractorError(f"Failed to fetch JDLO databases: {e}")
            
        song_entry = None
        for k, v in songs_db.items():
            if k.lower() == codename.lower():
                song_entry = v
                codename = k # Case correct
                break
                
        if not song_entry:
            raise JDLOExtractorError(f"Map '{codename}' not found in JDLO songs database.")
            
        pkg_name = f"{codename}_mapContent"
        pkg_entry = None
        for k, v in packages_db.items():
            if k.lower() == pkg_name.lower():
                pkg_entry = v
                break
                
        if not pkg_entry or "url" not in pkg_entry:
            raise JDLOExtractorError(f"Package URL for '{codename}' not found in JDLO packages database.")
            
        package_url = pkg_entry["url"]
        
        urls_map = self._client.get_content_authorization(codename)
        if not urls_map:
            raise JDLOExtractorError(f"Could not authorize content streaming for '{codename}'.")
            
        # 2. Identify Video and Audio URLs
        audio_url = None
        for k, v in urls_map.items():
            if k.lower().endswith(".ogg"):
                audio_url = v
                break

        video_url = None
        
        from jd2021_installer.core.config import QUALITY_ORDER, QUALITY_PATTERNS
        target_quality = self._config.video_quality.upper()
        if target_quality not in QUALITY_ORDER:
            target_quality = "ULTRA"
            
        start_index = QUALITY_ORDER.index(target_quality)
        fallback_order = QUALITY_ORDER[start_index:]
        
        for q in fallback_order:
            suffix = QUALITY_PATTERNS.get(q)
            if not suffix:
                continue
            for k, v in urls_map.items():
                if k.lower().endswith(suffix.lower()):
                    video_url = v
                    break
            if video_url:
                break
                
        if not video_url:
            for k, v in urls_map.items():
                if ".webm" in k.lower():
                    video_url = v
                    break
                    
        # 3. Download files
        pkg_dest = self._download_dir / f"{codename}_MAIN_SCENE.zip"
        if not pkg_dest.exists():
            self._download_file(package_url, pkg_dest)
            
        if audio_url:
            audio_dest = self._download_dir / f"{codename}.ogg"
            if not audio_dest.exists():
                self._download_file(audio_url, audio_dest)
                
        if video_url:
            # We must use the exact filename expected by normalizer, or just copy it with the correct suffix.
            # Normalizer expects something like {codename}_ULTRA.hd.webm or similar, but the actual URL might be lower quality.
            # Let's save it as the chosen suffix.
            # Wait, normalizer looks for .webm and picks it up.
            video_name = video_url.split("/")[-1].split("?")[0]
            video_dest = self._download_dir / video_name
            if not video_dest.exists():
                self._download_file(video_url, video_dest)
                
        # 3.5 Download assets (art)
        assets = song_entry.get("assets", {})
        for asset_key, asset_url in assets.items():
            if not asset_url:
                continue
            # Some URLs are empty strings or invalid
            if not asset_url.startswith("http"):
                continue
            asset_name = asset_url.split("/")[-1].split("?")[0]
            if asset_name.endswith((".tga.ckd", ".png", ".jpg")):
                asset_dest = self._download_dir / asset_name
                if not asset_dest.exists():
                    try:
                        self._download_file(asset_url, asset_dest)
                    except Exception as e:
                        logger.warning(f"Failed to download optional asset {asset_name}: {e}")

        # 4. Extract zip to extract_dir
        logger.info(f"Extracting {pkg_dest.name}...")
        try:
            with zipfile.ZipFile(pkg_dest, "r") as z:
                z.extractall(extract_dir)
        except Exception as e:
            raise JDLOExtractorError(f"Failed to extract ZIP package: {e}")
            
        from jd2021_installer.extractors.archive_ipk import ArchiveIPKExtractor
        for ipk_file in extract_dir.rglob("*.ipk"):
            logger.info(f"Extracting bundled IPK {ipk_file.name}...")
            try:
                ipk_ext = ArchiveIPKExtractor(ipk_file, desired_codename=codename)
                ipk_ext.extract(extract_dir)
                ipk_file.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Failed to extract bundled IPK {ipk_file.name}: {e}")
            
        # 5. Copy media to extract_dir for the normalizer
        for f in os.listdir(self._download_dir):
            if f.endswith(".zip"):
                continue
            src_file = self._download_dir / f
            dst_file = extract_dir / f
            if src_file.is_file() and not dst_file.exists():
                shutil.copy2(src_file, dst_file)
                
        # 6. Generate mock HTML files for offline usage
        assets_html = "<html><body>\n"
        assets_html += f'<a href="{codename}_MAIN_SCENE.zip">{codename}_MAIN_SCENE.zip</a><br>\n'
        for f in os.listdir(self._download_dir):
            if f.endswith(".zip") or f.endswith(".html"):
                continue
            assets_html += f'<a href="{f}">{f}</a><br>\n'
        assets_html += "</body></html>"
        
        nohud_html = "<html><body>\n"
        for f in os.listdir(self._download_dir):
            if ".webm" in f.lower():
                nohud_html += f'<a href="{f}">{f}</a><br>\n'
        nohud_html += "</body></html>"
        
        (self._download_dir / "assets.html").write_text(assets_html, encoding="utf-8")
        (self._download_dir / "nohud.html").write_text(nohud_html, encoding="utf-8")
        
        # Dump JDLO metadata for normalizer
        import json
        (self._download_dir / "jdlo_metadata.json").write_text(json.dumps(song_entry, indent=2), encoding="utf-8")
        
        shutil.copy2(self._download_dir / "assets.html", extract_dir / "assets.html")
        shutil.copy2(self._download_dir / "nohud.html", extract_dir / "nohud.html")
        shutil.copy2(self._download_dir / "jdlo_metadata.json", extract_dir / "jdlo_metadata.json")
                
        return extract_dir
