import base64
import json
import logging
import platform
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional, Tuple, Any

import requests

from jd2021_installer.core.config import AppConfig

logger = logging.getLogger("jd2021.core.jdlo_client")

def _get_dynamic_user_agent() -> str:
    commit_hash = "zip-build"
    try:
        kwargs = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], 
            capture_output=True, 
            text=True, 
            check=True,
            **kwargs
        )
        commit_hash = result.stdout.strip()
    except Exception:
        pass
        
    os_info = f"{platform.system()} {platform.release()}; {platform.machine()}"
    return f"JD2021MapInstaller/{commit_hash} ({os_info}) (+https://github.com/VenB304/jd2021-map-installer; ventralberry@gmail.com)"

class JDLOClient:
    """Client for interacting with JDLO's CDN and APIs."""
    
    BASE_URL = "https://jdlo.ovosimpatico.com"
    USER_AGENT_API = "UbiServices_SDK_2020.Release.17_PC64_ansi_static"
    USER_AGENT_DOWNLOAD = _get_dynamic_user_agent()
    
    _mem_cache_songs = None
    _mem_cache_songs_time = 0
    _mem_cache_sku = None
    _mem_cache_sku_time = 0
    
    def __init__(self, config: AppConfig):
        self.config = config
        self.cache_dir = config.cache_directory / "jdlo"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_auth_token(self) -> str:
        """Reads and decodes the TickedId from jdlo_auth.ini."""
        if not self.config.jdlo_auth_path or not self.config.jdlo_auth_path.exists():
            raise ValueError("JDLO auth file not configured or not found.")
            
        try:
            with open(self.config.jdlo_auth_path, "r", encoding="utf-8") as f:
                ini_content = f.read().strip()
                
            decoded = base64.b64decode(ini_content).decode("utf-8")
            fields = {}
            for line in decoded.strip().split("\n"):
                if "=" in line:
                    key, val = line.split("=", 1)
                    fields[key.strip()] = val.strip()
                    
            ticket = fields.get("TickedId")
            if not ticket:
                raise ValueError("TickedId not found in the decoded auth file.")
                
            return f"Ubi_v1 {ticket}"
        except Exception as e:
            logger.error(f"Failed to read JDLO auth token: {e}")
            raise ValueError(f"Invalid JDLO auth file: {e}")

    def _make_request(self, method: str, endpoint: str, headers: dict, **kwargs) -> requests.Response:
        """Makes an HTTP request with exponential backoff for 429 Too Many Requests."""
        url = f"{self.BASE_URL}{endpoint}"
        max_retries = self.config.max_retries
        base_delay = self.config.retry_base_delay_s
        
        for attempt in range(max_retries + 1):
            try:
                # Always enforce the installer's User-Agent on CDN requests as per agreement, 
                # but UbiServices for API where required (already passed in headers).
                # Actually, Ovo requested: "I would appreciate if you could add a custom User-Agent to your requests. Something like User-Agent: jd2021-map-installer/1.0.0 (+https://github.com/VenB304/jd2021-map-installer) would be perfect."
                # But UbiServices is strictly checked by the API, so we MUST use UbiServices for the API, 
                # and the custom User-Agent for downloading the actual zip/webm files. 
                # So we leave the headers as provided here.
                
                resp = requests.request(method, url, headers=headers, timeout=10, **kwargs)
                
                if resp.status_code == 429:
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(f"JDLO Rate limit hit. Retrying in {delay}s...")
                        time.sleep(delay)
                        continue
                    else:
                        resp.raise_for_status()
                        
                resp.raise_for_status()
                return resp
                
            except requests.RequestException as e:
                if attempt == max_retries:
                    raise e
                time.sleep(base_delay * (2 ** attempt))
                
        raise requests.RequestException("Max retries exceeded")

    def download_request(self, url: str, **kwargs) -> requests.Response:
        """Makes an HTTP request for downloads with exponential backoff for 429 Too Many Requests."""
        headers = kwargs.pop("headers", {})
        if "User-Agent" not in headers:
            headers["User-Agent"] = self.USER_AGENT_DOWNLOAD
            
        max_retries = self.config.max_retries
        base_delay = self.config.retry_base_delay_s
        
        for attempt in range(max_retries + 1):
            try:
                resp = requests.get(url, headers=headers, timeout=120, **kwargs)
                
                if resp.status_code == 429:
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(f"JDLO CDN Rate limit hit. Retrying in {delay}s...")
                        time.sleep(delay)
                        continue
                    else:
                        resp.raise_for_status()
                        
                resp.raise_for_status()
                return resp
                
            except requests.RequestException as e:
                if attempt == max_retries:
                    raise e
                time.sleep(base_delay * (2 ** attempt))
                
        raise requests.RequestException("Max retries exceeded")

    def fetch_songs_db(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Fetches the JDLO songs.json database, using cache if available."""
        if not force_refresh and JDLOClient._mem_cache_songs is not None:
            if time.time() - JDLOClient._mem_cache_songs_time < 86400:
                return JDLOClient._mem_cache_songs

        cache_file = self.cache_dir / "songs.json"
        
        # Cache for 24 hours
        if not force_refresh and cache_file.exists():
            if time.time() - cache_file.stat().st_mtime < 86400:
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        JDLOClient._mem_cache_songs = data
                        JDLOClient._mem_cache_songs_time = time.time()
                        return data
                except json.JSONDecodeError:
                    pass
                    
        logger.info("Fetching JDLO songs database...")
        token = self.get_auth_token()
        headers = {
            "Authorization": token,
            "User-Agent": self.USER_AGENT_API,
            "X-SkuId": "jd2017-pc-ww" # Use 2017 to get modern maps
        }
        
        resp = self._make_request("GET", "/songdb/v2/songs", headers)
        data = resp.json()
        
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
            
        self._update_last_fetched("songs")
        JDLOClient._mem_cache_songs = data
        JDLOClient._mem_cache_songs_time = time.time()
        return data

    def fetch_sku_packages(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Fetches and merges sku-packages (Durango + PC fallback)."""
        if not force_refresh and JDLOClient._mem_cache_sku is not None:
            if time.time() - JDLOClient._mem_cache_sku_time < 86400:
                return JDLOClient._mem_cache_sku

        cache_file = self.cache_dir / "sku-packages.json"
        
        if not force_refresh and cache_file.exists():
            if time.time() - cache_file.stat().st_mtime < 86400:
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        JDLOClient._mem_cache_sku = data
                        JDLOClient._mem_cache_sku_time = time.time()
                        return data
                except json.JSONDecodeError:
                    pass
                    
        logger.info("Fetching JDLO sku-packages...")
        token = self.get_auth_token()
        
        # 1. Fetch JD2017 PC (Fallback / Modern maps)
        headers_pc = {
            "Authorization": token,
            "User-Agent": self.USER_AGENT_API,
            "X-SkuId": "jd2017-pc-ww"
        }
        resp_pc = self._make_request("GET", "/packages/v1/sku-packages", headers_pc)
        merged_packages = resp_pc.json()
        
        # 2. Fetch JD2021 Durango (Primary, will overwrite PC where they overlap)
        headers_durango = {
            "Authorization": token,
            "User-Agent": self.USER_AGENT_API,
            "X-SkuId": "jd2021-pc-all"
        }
        resp_durango = self._make_request("GET", "/packages/v1/sku-packages", headers_durango)
        
        # Merge Durango on top
        for k, v in resp_durango.json().items():
            merged_packages[k] = v
            
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(merged_packages, f)
            
        self._update_last_fetched("sku_packages")
        JDLOClient._mem_cache_sku = merged_packages
        JDLOClient._mem_cache_sku_time = time.time()
        return merged_packages

    def get_content_authorization(self, codename: str) -> Dict[str, str]:
        """Gets streaming URLs for a specific map from content-authorization."""
        token = self.get_auth_token()
        headers = {
            "Authorization": token,
            "User-Agent": self.USER_AGENT_API,
            "X-SkuId": "jd2021-pc-all" # Required for some reason for streaming links
        }
        
        try:
            resp = self._make_request("GET", f"/content-authorization/v1/maps/{codename}", headers)
            data = resp.json()
            return data.get("urls", {})
        except requests.RequestException as e:
            logger.warning(f"Content authorization failed for {codename}: {e}")
            # Try fallback
            return self._get_fallback_urls(codename)
            
    def _get_fallback_urls(self, codename: str) -> Dict[str, str]:
        return {
            f"jmcs://jd-contents/{codename}/{codename}_ULTRA.hd.webm": f"https://cdn.ovosimpatico.com/jdlo/maps/{codename}/{codename}_ULTRA.hd.webm",
            f"jmcs://jd-contents/{codename}/{codename}_ULTRA.webm": f"https://cdn.ovosimpatico.com/jdlo/maps/{codename}/{codename}_ULTRA.webm",
            f"jmcs://jd-contents/{codename}/{codename}_HIGH.webm": f"https://cdn.ovosimpatico.com/jdlo/maps/{codename}/{codename}_HIGH.webm",
            f"jmcs://jd-contents/{codename}/{codename}_MID.webm": f"https://cdn.ovosimpatico.com/jdlo/maps/{codename}/{codename}_MID.webm",
            f"jmcs://jd-contents/{codename}/{codename}_LOW.webm": f"https://cdn.ovosimpatico.com/jdlo/maps/{codename}/{codename}_LOW.webm",
            f"jmcs://jd-contents/{codename}/{codename}.ogg": f"https://cdn.ovosimpatico.com/jdlo/maps/{codename}/{codename}.ogg",
        }

    def extend_cache(self):
        "Touches cache files to extend their 24h lifespan."
        for file in ['songs.json', 'sku-packages.json']:
            path = self.cache_dir / file
            if path.exists():
                import os
                os.utime(path, None)

    def invalidate_cache(self):
        "Deletes cache files to force refresh on next run."
        for file in ['songs.json', 'sku-packages.json', 'cache_meta.json']:
            path = self.cache_dir / file
            if path.exists():
                try:
                    path.unlink()
                except Exception:
                    pass

    def _update_last_fetched(self, key: str):
        """Updates the last fetched timestamp for a given database key in cache_meta.json."""
        meta_file = self.cache_dir / "cache_meta.json"
        data = {}
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
        data[key] = time.time()
        try:
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

    def get_last_fetched(self, key: str) -> float:
        """Retrieves the last fetched timestamp for a given database key from cache_meta.json."""
        meta_file = self.cache_dir / "cache_meta.json"
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return float(data.get(key, 0.0))
            except Exception:
                pass
        return 0.0
