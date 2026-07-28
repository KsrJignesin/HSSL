"""
Hotstar (Disney+ Hotstar) Service for unshackle
Based on vinetrimmer Hotstar by @KsrJignesin
Uses QR code activation login, curl_cffi for manifest downloads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from typing import Any, Optional
from urllib.parse import urljoin

import click

from unshackle.core.credential import Credential
from unshackle.core.manifests import DASH
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from unshackle.core.tracks import Chapter, Chapters, Subtitle, Tracks
from unshackle.core.utils.xml import load_xml

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

log = logging.getLogger(__name__)


class JHS(Service):
    """
    Service code for Disney+ Hotstar (https://hotstar.com).

    Authorization: QR code device activation (no password needed).
    Uses device code + polling for login.

    Example:
        unshackle dl JHS https://www.hotstar.com/in/movies/12345
        unshackle dl JHS https://www.hotstar.com/in/tv/show/67890
    """

    ALIASES = ("JHS", "Hotstar", "hotstar")
    TITLE_RE = r"(?:https?://(?:www\.)?hotstar\.com/[a-z0-9/-]+/)(?P<id>\d+)"

    @staticmethod
    @click.command(name="JHS", short_help="https://hotstar.com (Disney+ Hotstar)")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return JHS(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        self.title = title
        super().__init__(ctx)

        if curl_requests is None:
            raise ImportError("JHS requires 'curl_cffi'. Install: pip install curl-cffi")

        self.parse_title(title)
        self.content_type = "MOVIE" if "/movies/" in title else "TV"
        self.device_id = None
        self.token_data = {}
        self.license_api = None
        self.region = "in"

        self.configure()

    def parse_title(self, title: str):
        """Extract content ID from URL."""
        m = re.search(self.TITLE_RE, title)
        if m:
            self.content_id = m.group("id")
        else:
            self.content_id = title

    def configure(self):
        """Initialize session headers and authenticate."""
        self.session.headers.update({
            "Origin": "https://www.hotstar.com",
            "Referer": "https://www.hotstar.com/in",
        })
        self.get_token()

    def get_token(self):
        """Get or renew auth token via QR code or cookie auto-activation."""
        self.device_id = secrets.token_hex(8)
        headers = {
            "Accept-Encoding": "gzip",
            "Accept-Language": "eng",
            "app_name": "android",
            "Connection": "Keep-Alive",
            "Content-Type": "application/json",
            "User-Agent": "Hotstar;in.startv.hotstar/26.06.08.6.4557 (Android/12)",
            "X-HS-Accept-Language": "eng",
            "X-HS-App": "4557",
            "X-HS-Client": "platform:androidtv;app_id:in.startv.hotstar;app_version:26.06.08.6;os:Android;os_version:12;schema_version:0.0.1705;brand:Sony;model:BRAVIA_4K_GB",
            "X-HS-Client-Targeting": f"ad_id:;user_lat:false;hw_id:{self.device_id}",
            "X-HS-Device-Id": self.device_id,
            "X-HS-Platform": "androidtv",
            "X-HS-Schema-Version": "0.0.1705",
        }
        self.session.headers.update(headers)

        # Get proxy state
        try:
            loc_resp = self.session.get("https://usersvc.hotstar.com/v1/location")
            proxy_state = loc_resp.headers.get("x-hs-setproxystate-ud", "")
        except Exception:
            proxy_state = ""

        self.session.headers["X-Hs-ProxyState-ud"] = proxy_state
        self.session.headers["X-HS-Is-Retry"] = "false"

        # Fresh start
        client_caps = json.dumps({
            "package": ["dash", "hls"],
            "container": ["fmp4", "ts", "fmp4br"],
            "ads": ["non_ssai", "ssai"],
            "audio_channel": ["dolbyatmos", "atmos", "dolby51", "stereo"],
            "encryption": ["plain", "widevine"],
            "video_codec": ["h264"],
            "ladder": ["phone", "tv"],
            "resolution": ["sd", "hd", "fhd", "4k"],
            "dynamic_range": ["SDR"]
        }, separators=(",", ":"))

        params = {
            "client_capabilities": client_caps,
            "drm_parameters": json.dumps({
                "widevine_security_level": ["SW_SECURE_DECODE", "SW_SECURE_CRYPTO"],
                "hdcp_version": ["HDCP_NONE"]
            }),
        }

        payload = json.dumps({
            "2": "",
            "6": 1,
            "7": {
                "1": {"1": self.device_id, "2": 3},
                "2": {"2": "Android", "3": "31"}
            }
        }, separators=(",", ":"))

        try:
            fresh = self.session.post("https://apix.hotstar.com/v2/freshstart", params=params, data=payload.encode())
            user_token = fresh.headers.get("x-hs-updatedusertoken") or fresh.headers.get("x-hs-updatedUserToken", "")
            if user_token:
                self.session.headers["X-Hs-UserToken"] = user_token

            # Start and get QR code
            start_resp = self.session.post("https://apix.hotstar.com/v2/start", params=params,
                                           data=json.dumps({"2": "", "6": 2}).encode())
            self.session.headers["X-Hs-ProxyState"] = start_resp.headers.get("x-hs-setproxystate", "")

            # Extract QR URL from response
            data = start_resp.json()
            try:
                widgets = data["success"]["page"]["spaces"]["content"]["widget_wrappers"][0]["widget"]["data"]["widgets"]
                hero = widgets[0]["hero_widget"]
                qr_url = "https://apix.hotstar.com" + hero["widget_commons"]["data_bind_mechanism"]["centralStore"]["http_request_commons"]["url"]
                qr_img = hero["data"]["illustration"]["image"]["src"]

                # Try auto-activation using cookies from default location
                from pathlib import Path as _Path
                from unshackle.core.config import config as _cfg
                cookie_file = _cfg.directories.cookies / "JHS" / "default.txt"
                cookie_activated = False
                if cookie_file.exists():
                    log.info(" + Found JHS cookie file, attempting auto-activation...")
                    try:
                        cookies_str = ""
                        session_token = None
                        for line in cookie_file.read_text("utf-8", errors="ignore").splitlines():
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            parts = line.split("\t")
                            if len(parts) >= 7:
                                name = parts[5]
                                value = parts[6]
                                if cookies_str:
                                    cookies_str += "; "
                                cookies_str += f"{name}={value}"
                                if name == "sessionUserUP":
                                    session_token = value
                        if session_token:
                            cookies_str += f"; userUP={session_token}"

                        if cookies_str:
                            import requests as _req
                        act_headers = {
                            "authority": "www.hotstar.com",
                            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                            "accept-language": "en-US,en;q=0.9",
                            "cookie": cookies_str,
                            "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
                            "sec-ch-ua": '"Chromium";v="131", "Not;A=Brand";v="99"',
                            "sec-ch-ua-mobile": "?1",
                            "sec-ch-ua-platform": '"Android"',
                        }
                        act_resp = _req.get(qr_url, headers=act_headers, timeout=30)
                        if act_resp.status_code == 200:
                            log.info(" + Auto-activation via cookies successful!")
                            cookie_activated = True
                    except Exception as e:
                        log.debug(f" + Auto-activation failed: {e}")

                if not cookie_activated:
                    log.info(f"LOGIN_REQUIRED|{qr_img}|0|300")
                    log.info(f" + Scan QR code with Hotstar app or visit: hotstar.com/activate")
                    print(f"LOGIN_REQUIRED|{qr_img}|0|300")

                # Poll for login
                for _ in range(300):
                    try:
                        poll = self.session.post(qr_url, data=json.dumps({"1": ""}).encode())
                        login_state = poll.headers.get("x-hs-user-login-state")
                        if login_state == "LOGGEDIN":
                            token = poll.headers.get("x-hs-updatedusertoken")
                            if token:
                                self.session.headers["X-Hs-UserToken"] = token
                                self.token_data = dict(self.session.headers)
                                log.info("LOGIN_SUCCESS")
                                return
                    except Exception:
                        pass
                    time.sleep(1)
                log.info("LOGIN_TIMEOUT")
            except (KeyError, IndexError) as e:
                log.warning(f" + QR login flow failed: {e}")
        except Exception as e:
            log.warning(f" + Token init failed: {e}")

    def authenticate(self, cookies=None, credential: Optional[Credential] = None):
        """Called by unshackle."""
        if credential:
            log.warning(" + Hotstar uses QR activation, not username/password")

    def get_titles(self) -> Titles_T:
        url = self.config.get("endpoints", {}).get("movie_title", "https://apix.hotstar.com/v2/pages/detail")
        client_caps = json.dumps({
            "package": ["dash", "hls"],
            "container": ["fmp4", "ts", "fmp4br"],
            "ads": ["non_ssai", "ssai"],
            "audio_channel": ["dolbyatmos", "atmos", "dolby51", "stereo"],
            "encryption": ["plain", "widevine"],
            "ladder": ["tv"],
            "resolution": ["4k"]
        }, separators=(",", ":"))

        params = {
            "content_id": self.content_id,
            "client_capabilities": client_caps,
            "drm_parameters": json.dumps({
                "widevine_security_level": ["SW_SECURE_DECODE", "SW_SECURE_CRYPTO"],
                "hdcp_version": ["HDCP_NONE"]
            }),
        }

        resp = self.session.get(url, params=params)
        if not resp.ok:
            raise SystemExit(f" - Failed to get title: HTTP {resp.status_code}")

        data = resp.json()

        if self.content_type == "MOVIE":
            # Extract movie info
            try:
                content = data["success"]["page"]["spaces"]["spotlight"]["widget_wrappers"][0]["widget"]["data"]["content_info"]
                name = content.get("title", "Unknown")
                year = ""
                for tag in content.get("core_meta_tags", []):
                    if tag.get("value", "").isdigit():
                        year = tag["value"]
                        break
                return Movies([Movie(
                    id_=self.content_id,
                    name=name,
                    year=year,
                    service=self.__class__,
                    data=content,
                )])
            except (KeyError, IndexError) as e:
                raise SystemExit(f" - Failed to parse movie data: {e}")
        else:
            # TV Show - extract episodes
            episodes = []
            try:
                tabs = data["success"]["page"]["spaces"]["tabbed"]["widget_wrappers"][0]["widget"]["data"]["category_picker"]["data"]["tabs"]
                for tab in tabs:
                    tray_url = "https://apix.hotstar.com" + tab["tab"]["data"]["tray_widget_url"]
                    tray_resp = self.session.get(tray_url).json()
                    items = tray_resp.get("success", {}).get("widget_wrapper", {}).get("widget", {}).get("data", {}).get("items", [])

                    for item in items:
                        cd = item.get("playable_content", {}).get("data", {})
                        ep_id = cd.get("content_id", "")
                        ep_title = cd.get("title", f"Episode")
                        tags = cd.get("tags", [])
                        season_num, ep_num = 0, 0
                        for tag in tags:
                            val = tag.get("value", "")
                            m = re.search(r"S(\d+).?E(\d+)", val, re.I)
                            if m:
                                season_num = int(m.group(1))
                                ep_num = int(m.group(2))
                        if ep_id:
                            episodes.append(Episode(
                                id_=ep_id,
                                title=cd.get("show_title", content.get("title", "")),
                                name=ep_title,
                                season=season_num,
                                number=ep_num,
                                service=self.__class__,
                                data=cd,
                            ))
            except (KeyError, IndexError) as e:
                raise SystemExit(f" - Failed to parse episodes: {e}")

            return Series(episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        watch_url = self.config.get("endpoints", {}).get("watch", "https://apix.hotstar.com/v2/pages/watch")

        # Determine codec/range
        params = {
            "content_id": self.content_id,
            "client_capabilities": json.dumps({
                "package": ["dash"],
                "container": ["fmp4"],
                "ads": ["non_ssai", "ssai"],
                "audio_channel": ["dolbyatmos", "atmos", "dolby51", "stereo"],
                "encryption": ["plain", "widevine"],
                "video_codec": ["h265"],
                "ladder": ["tv"],
                "resolution": ["4k"],
                "dynamic_range": ["sdr"]
            }, separators=(",", ":")),
            "drm_parameters": json.dumps({
                "widevine_security_level": ["SW_SECURE_DECODE", "SW_SECURE_CRYPTO"],
                "hdcp_version": ["HDCP_NONE"]
            }),
            "request_features": "consent_supported",
        }

        try:
            resp = self.session.get(watch_url, params=params, timeout=45)
            data = resp.json()
            playback = data["success"]["page"]["spaces"]["player"]["widget_wrappers"][0]["widget"]["data"]["player_config"]["media_asset"]["primary"]
            manifest_url = playback["content_url"]

            # Get license URL
            licence_urls = data["success"]["page"]["spaces"]["player"]["widget_wrappers"][0]["widget"]["data"]["player_config"]["media_asset"].get("licence_urls")
            if licence_urls and len(licence_urls) > 0:
                self.license_api = licence_urls[0]
        except (KeyError, IndexError, TypeError) as e:
            raise SystemExit(f" - Manifest fetch failed: {e}")

        log.info(f" + Manifest: {manifest_url[:80]}...")

        # Download manifest with curl_cffi (Hotstar requires specific headers)
        proxy = self.session.proxies.get("all")
        ses = curl_requests.Session(impersonate="chrome")
        manifest_resp = ses.get(manifest_url, headers=dict(self.session.headers),
                                proxies={"http": proxy, "https": proxy} if proxy else None)
        if manifest_resp.status_code != 200:
            raise SystemExit(f" - Manifest download failed: HTTP {manifest_resp.status_code}")

        manifest_text = manifest_resp.text
        mpd_cookies = dict(manifest_resp.cookies)

        # Build cookie string
        cookie_parts = []
        for k in ("CloudFront-Key-Pair-Id", "CloudFront-Policy", "CloudFront-Signature", "hdntl"):
            if k in mpd_cookies:
                cookie_parts.append(f"{k}={mpd_cookies[k]}")
        if cookie_parts:
            self.session.headers["Cookie"] = "; ".join(cookie_parts)

        # Parse MPD
        tracks = Tracks(list(DASH.from_url(url=manifest_url, session=self.session).to_tracks(language="en")))

        for track in tracks:
            if track.drm and self.license_api:
                pass  # License handled via get_widevine_license
            if hasattr(track, 'language') and str(track.language) == "und":
                track.language = title.language or "eng"

        for sub in tracks.subtitles:
            if str(sub.language) == "en":
                sub.sdh = True

        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()

    def certificate(self, **kwargs):
        return None

    def get_widevine_license(self, challenge: bytes, title: Title_T, track: Any) -> bytes:
        if not self.license_api:
            raise SystemExit(" - No license URL available")

        ses = curl_requests.Session(impersonate="chrome")
        proxy = self.session.proxies.get("all")
        headers = {
            "Content-Type": "application/octet-stream",
            "User-Agent": self.token_data.get("User-Agent", "Hotstar;in.startv.hotstar/26.06.08.6.4557 (Android/12)"),
            "X-HS-Device-Id": self.device_id,
            "X-Hs-UserToken": self.session.headers.get("X-Hs-UserToken", ""),
            "hotstarauth": self.session.headers.get("hotstarauth", ""),
        }

        resp = ses.post(self.license_api, data=challenge, headers=headers,
                        proxies={"http": proxy, "https": proxy} if proxy else None)
        return resp.content
