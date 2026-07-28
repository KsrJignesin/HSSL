import base64
import json
import os
import queue
import random
import re
import secrets
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import click
import m3u8
import requests

from vinetrimmer.config import config, directories
from vinetrimmer.objects import TextTrack, Title, Tracks
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.pypr import PSSH, Cdm, Device, PlayReadyHeaderBuilder
from vinetrimmer.utils.widevine.device import LocalDevice


class Sonyliv(BaseService):
    """
    SonyLiv India streaming service (https://sonyliv.com).
    \b
    Authorization: OTP + Android TV device activation
    Security: UHD@L3

    Script By Hugov, https://telegram.me/divine_404, mhm
    """

    ALIASES = ["SL", "sonyliv"]

    TITLE_RE = r"^(?:https?://(?:www\.)?sonyliv.com/(?P<type>movies|shows)/[a-z0-9-]+-)?(?P<id>\d+)"

    @staticmethod
    @click.command(name="Sonyliv", short_help="https://sonyliv.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Sonyliv(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.m = self.parse_title(ctx, title)

        self.vcodec = ctx.parent.params["vcodec"] or "H264"
        self.acodec = ctx.parent.params["acodec"] or "EC3"
        self.range = ctx.parent.params["range_"] or "SDR"
        self.quality = ctx.parent.params.get("quality") or 1080
        self.playready = ctx.obj.cdm.device.type == LocalDevice.Types.PLAYREADY

        self.profile = ctx.obj.profile

        android_cfg = self.config.get("device", {}).get("android", {})
        web_cfg = self.config.get("device", {}).get("web", {})

        self.android_user_agent = android_cfg.get(
            "user_agent",
            "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
        )
        self.android_build_number = android_cfg.get("build_number", "10491")
        self.android_app_version = android_cfg.get("app_version", "6.12.35")

        self.web_user_agent = web_cfg.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0",
        )
        self.web_accept_language = web_cfg.get("accept_language", "en-US,en;q=0.9")
        self.web_sec_ch_ua = web_cfg.get(
            "sec_ch_ua",
            '"Microsoft Edge";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        )

        self.device_id = None
        self.session_id = None
        self.security_token = None
        self.accessToken = None
        self.profile_id = None
        self.license_api = None

        self.configure()

    def _android_headers(self):
        return {
            "Content-Type": "application/json",
            "x-via-device": "true",
            "Host": "apiv2.sonyliv.com",
            "Authorization": self.accessToken,
            "session-id": self.session_id,
            "user-agent": self.android_user_agent,
            "build_number": self.android_build_number,
            "app_version": self.android_app_version,
            "security_token": self.security_token,
            "device_id": self.device_id,
        }

    def get_titles(self):
        headers = self._android_headers()

        r = requests.get(
            url=self.config["endpoints"]["title"].format(id=self.m["id"]),
            headers=headers,
            proxies=self.session.proxies,
        )
        try:
            titleRes = json.loads(r.content.decode())
        except json.JSONDecodeError:
            raise ValueError(f"Received Irrelevant Title API Response: {r.text}")

        for correct in titleRes["resultObj"]["containers"]:
            if int(correct["id"]) == int(self.m["id"]):
                titleRes = correct.copy()

        if (
            titleRes["metadata"]["objectSubtype"] == "MOVIE_BUNDLE"
            or titleRes["metadata"]["objectSubtype"] == "MOVIE"
        ):
            return Title(
                id_=self.m["id"],
                type_=Title.Types.MOVIE,
                name=titleRes["metadata"]["title"],
                year=titleRes["metadata"]["emfAttributes"]["release_year"]
                if "release_year" in titleRes["metadata"]["emfAttributes"]
                else titleRes["metadata"]["emfAttributes"]["release_date"].split("-")[
                    0
                ],
                original_lang=titleRes["metadata"]["language"],
                source=self.ALIASES[0],
                service_data=titleRes,
            )

        elif titleRes["layout"] == "BUNDLE_ITEM":
            bucket = []

            if titleRes["metadata"]["objectSubtype"] == "EPISODIC_SHOW":
                ep_count = titleRes["episodeCount"]
                r = requests.get(
                    url=self.config["endpoints"]["season"].format(
                        id=titleRes["id"], ep_start=0, ep_end=ep_count - 1
                    ),
                    headers=headers,
                    proxies=self.session.proxies,
                )

                try:
                    seasonRes = json.loads(r.content.decode())
                except json.JSONDecodeError:
                    raise ValueError(
                        f"Received Irrelevant Season API Response: {r.text}"
                    )

                for episode in seasonRes["resultObj"]["containers"][0]["containers"]:
                    bucket.append(
                        {
                            "episode_id": episode["id"],
                            "series_name": titleRes["metadata"]["title"],
                            "season_number": titleRes["metadata"]["season"]
                            if ("season" in titleRes["metadata"].keys())
                            else "1",
                            "episode_number": episode["metadata"]["episodeNumber"],
                            "episode_name": episode["metadata"]["episodeTitle"],
                            "episode_org_lang": episode["metadata"]["language"],
                            "service_data": episode,
                        }
                    )

            if titleRes["metadata"]["objectSubtype"] == "SHOW":
                for season in titleRes["containers"]:
                    if season["metadata"]["objectSubtype"] == "SEASON" and int(
                        season["parents"][0]["parentId"]
                    ) == int(self.m["id"]):
                        ep_count = season["episodeCount"]
                        r = requests.get(
                            url=self.config["endpoints"]["season"].format(
                                id=season["id"], ep_start=0, ep_end=ep_count - 1
                            ),
                            headers=headers,
                            proxies=self.session.proxies,
                        )
                        try:
                            seasonRes = json.loads(r.content.decode())
                        except json.JSONDecodeError:
                            raise ValueError(
                                f"Received Irrelevant Season API Response: {r.text}"
                            )

                        for episode in seasonRes["resultObj"]["containers"][0][
                            "containers"
                        ]:
                            bucket.append(
                                {
                                    "episode_id": episode["id"],
                                    "series_name": titleRes["metadata"]["title"],
                                    "season_number": season["metadata"]["season"],
                                    "episode_number": episode["metadata"][
                                        "episodeNumber"
                                    ],
                                    "episode_name": episode["metadata"]["episodeTitle"],
                                    "episode_org_lang": episode["metadata"]["language"],
                                    "service_data": episode,
                                }
                            )

                    elif season["metadata"]["objectSubtype"] == "EPISODE_RANGE" and int(
                        season["parents"][0]["parentId"]
                    ) == int(self.m["id"]):
                        ep_count = season["episodeCount"]
                        r = requests.get(
                            url=self.config["endpoints"]["season"].format(
                                id=season["id"], ep_start=0, ep_end=ep_count - 1
                            ),
                            headers=headers,
                            proxies=self.session.proxies,
                        )

                        try:
                            seasonRes = json.loads(r.content.decode())
                        except json.JSONDecodeError:
                            raise ValueError(
                                f"Received Irrelevant Season API Response: {r.text}"
                            )

                        for episode in seasonRes["resultObj"]["containers"][0][
                            "containers"
                        ]:
                            bucket.append(
                                {
                                    "episode_id": episode["id"],
                                    "series_name": titleRes["metadata"]["title"],
                                    "season_number": season["metadata"]["season"],
                                    "episode_number": episode["metadata"][
                                        "episodeNumber"
                                    ],
                                    "episode_name": episode["metadata"]["episodeTitle"],
                                    "episode_org_lang": episode["metadata"]["language"],
                                    "service_data": episode,
                                }
                            )

            if not bucket == []:
                return [
                    Title(
                        id_=b["episode_id"],
                        type_=Title.Types.TV,
                        name=b["series_name"],
                        season=b["season_number"],
                        episode=b["episode_number"],
                        episode_name=b["episode_name"],
                        original_lang=b["episode_org_lang"],
                        source=self.ALIASES[0],
                        service_data=b["service_data"],
                    )
                    for b in bucket
                ]
        else:
            self.log.exit(" - Title unsupported.")

    def get_tracks(self, title):
        hdr_map = {
            "DV": "DOLBY_VISION",
            "HDR10": "HDR10",
            "SDR": "HLG",
        }

        td_client_hints = None
        if self.vcodec == "H265":
            td_client_hints = {
                "device_make": "Amazon",
                "device_model": "AFTMM",
                "display_res": "2160",
                "viewport_res": "2160",
                "supp_codec": "HEVC,H264,AAC,EAC3,AC3,ATMOS",
                "audio_decoder": "EAC3,AAC,AC3,ATMOS",
                "hdr_decoder": hdr_map.get(self.range, "HLG"),
                "td_user_useragent": self.android_user_agent,
            }

        headers = self._android_headers()
        if td_client_hints:
            headers["Td_client_hints"] = json.dumps(
                td_client_hints, separators=(",", ":")
            )

        if str(title.type) == "Types.MOVIE":
            if "containers" not in title.service_data.keys():
                _id_ = title.service_data["metadata"]["contentId"]
            else:
                for mov in title.service_data["containers"]:
                    if mov["metadata"]["contentSubtype"] == "MOVIE":
                        _id_ = mov["id"]
        else:
            _id_ = title.service_data["metadata"]["contentId"]

        r = requests.get(
            url=self.config["endpoints"]["manifest"].format(id=_id_),
            headers=headers,
            proxies=self.session.proxies,
        )
        try:
            manifestRes = json.loads(r.content.decode())
            self.log.debug(f"\n{json.dumps(manifestRes, indent=4)}")
            if manifestRes.get("resultCode") == "KO":
                error_code = manifestRes.get("errorDescription")
                message = manifestRes.get("message", "Unknown error")
                if error_code == "ACN_2411":
                    limit = (manifestRes.get("resultObj") or {}).get(
                        "MaxAllowedConcurrencyLimit"
                    )
                    self.log.exit(
                        f" - Concurrent stream limit reached. Max allowed: {limit}"
                    )
                self.log.exit(f" - {message}")
        except json.JSONDecodeError:
            raise ValueError(f"Received Irrelevant Manifest API Response: {r.text}")

        video_result = manifestRes.get("resultObj") or {}
        mpd_url = video_result.get("videoURL")

        if not mpd_url:
            playback_items = video_result.get("playbackItems") or []
            if playback_items and isinstance(playback_items[0], dict):
                mpd_url = playback_items[0].get("url")

        if not mpd_url:
            self.log.exit(" - MPD URL not found in manifest response.")

        browser_value = "ie" if self.playready else "chrome"
        license_r = requests.post(
            url=self.config["endpoints"]["license"],
            headers=headers,
            json={
                "actionType": "play",
                "assetId": str(_id_),
                "browser": browser_value,
                "deviceId": self.device_id,
                "os": "android",
                "platform": "web",
            },
            proxies=self.session.proxies,
        )
        try:
            license_json = license_r.json()
            if license_json.get("resultCode") == "KO":
                self.log.warning(
                    f" - License URL request failed: {license_json.get('errorDescription')} - "
                    f"{license_json.get('message', 'Unknown error')}"
                )
            else:
                license_result = license_json.get("resultObj") or {}
                license_url = license_result.get("laURL")
                license_token = license_result.get("token")
                if license_url and license_token:
                    self.license_api = f"{license_url}?ExpressPlayToken={license_token}"
                elif license_url and "ExpressPlayToken" in license_url:
                    self.license_api = license_url
                elif license_url:
                    self.log.warning(
                        f" - License URL request returned no token: {license_url}"
                    )
                    self.license_api = license_url
        except Exception:
            pass

        self.session.headers.update(
            {
                "User-Agent": self.web_user_agent,
                "Accept": "*/*",
                "Accept-Language": self.web_accept_language,
                "Accept-Encoding": "gzip, deflate",
                "Referer": "https://www.sonyliv.com/",
                "X-Playback-Session-Id": f"{uuid.uuid4().hex}-{int(time.time() * 1000)}",
                "Origin": "https://www.sonyliv.com",
            }
        )

        r = self.session.get(mpd_url)

        if ".m3u8" not in str(mpd_url):
            tracks = Tracks.from_mpd(
                url=mpd_url,
                data=r.content.decode(),
                session=self.session,
                source=self.ALIASES[0],
            )
        else:
            tracks = Tracks.from_m3u8(
                m3u8.loads(str(r.content.decode())),
                source=self.ALIASES[0],
            )

        for video in tracks.videos:
            video.hdr10 = False
            video.dv = False
            video.hlg = False

        av_range_ = (video_result.get("additionalDataJson") or {}).get(
            "video_quality", ""
        )
        if av_range_ == "HDR":
            for video in tracks.videos:
                video.hdr10 = True
        elif av_range_ == "DOLBY_VISION":
            for video in tracks.videos:
                video.dv = True
        elif av_range_ == "HLG":
            for video in tracks.videos:
                video.hlg = True

        # Adding subtitle tracks
        for sub in video_result.get("subtitle") or []:
            tracks.add(
                TextTrack(
                    id_=sub["subtitleId"],
                    source=self.ALIASES[0],
                    url=sub["subtitleUrl"],
                    codec="vtt",
                    language=sub["subtitleLanguageName"],
                ),
                warn_only=True,
            )

        # PlayReady: extract keys using pypr before returning tracks
        if self.playready and self.license_api:
            self._extract_playready_keys(r.content.decode(), tracks)

        for track in tracks:
            track.needs_proxy = True
        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def _find_prd_device(self):
        cdm_name = (
            config.cdm.get("Sonyliv")
            or config.cdm.get("sonyliv")
            or config.cdm.get("default")
        )
        if not cdm_name:
            self.log.exit(" - No CDM device configured")

        device_dir = Path(directories.devices)

        prd_path = device_dir / f"{cdm_name}.prd"
        if prd_path.exists():
            return Device.load(prd_path)

        cdm_dir = device_dir / cdm_name
        if cdm_dir.is_dir():
            prd_files = list(cdm_dir.glob("*.prd"))
            if prd_files:
                return Device.load(max(prd_files, key=lambda x: x.stat().st_mtime))

        prd_files = list(device_dir.glob("**/*.prd"))
        if prd_files:
            return Device.load(max(prd_files, key=lambda x: x.stat().st_mtime))

        self.log.exit(" - No PlayReady .prd device file found")

    def _get_playready_keys(self, kid_hex):
        """
        Use pypr to get PlayReady content keys for a given KID.
        Generates PSSH from KID (original PSSH often fails for playready), creates
        a challenge via pypr CDM, sends to the license server, and
        returns a list of (kid_hex, key_hex) tuples.
        """
        # Build PlayReady header from KID
        normalized_kid = kid_hex.replace("-", "").strip()
        header = PlayReadyHeaderBuilder(normalized_kid).build_header(
            version="4.0",
            header_spec=None,
            encryption_scheme="cenc",
            key_specs=[(normalized_kid, normalized_kid)],
        )
        playready_header = base64.b64encode(header).decode("ascii")

        device = self._find_prd_device()
        cdm = Cdm.from_device(device)
        session_id = cdm.open()

        try:
            pssh = PSSH(playready_header)
            challenge = cdm.get_license_challenge(session_id, pssh.wrm_headers[0])

            if isinstance(challenge, bytes):
                challenge = challenge.decode("utf-8")

            response = requests.post(
                url=self.license_api,
                headers={
                    "accept": "*/*",
                    "origin": "https://www.sonyliv.com",
                    "referer": "https://www.sonyliv.com/",
                    "soapaction": '"http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense"',
                    "user-agent": self.web_user_agent,
                },
                data=challenge,
                timeout=30,
            )
            response.raise_for_status()

            response_text = response.text
            if not response_text:
                response_text = response.content.decode("utf-8", errors="replace")

            cdm.parse_license(session_id, response_text)
            keys = []
            for key in cdm.get_keys(session_id):
                keys.append((key.key_id.hex, key.key.hex()))
            return keys
        finally:
            cdm.close(session_id)

    def _extract_playready_keys(self, mpd_content, tracks):
        """
        Get KIDs from the mpd, get PR content keys via pypr,
        and pre set track.key so dl.py skips its CDM pipeline (we want to decrypt using pypr)
        """
        content_keys = {}
        try:
            root = ET.fromstring(mpd_content)
        except ET.ParseError:
            self.log.warning(" - Failed to parse MPD XML for PlayReady KID extraction")
            return

        kids = set()
        for cp in root.iter():
            kid = cp.attrib.get("{urn:mpeg:cenc:2013}default_KID")
            if kid:
                kids.add(kid.replace("-", "").lower())

        if not kids:
            self.log.warning(" - No KIDs found in MPD for PlayReady")
            return

        self.log.info(f" + Found {len(kids)} KID(s) in MPD for PlayReady")

        for kid in kids:
            try:
                keys = self._get_playready_keys(kid)
                for key_kid, key_value in keys:
                    normalized = key_kid.replace("-", "").lower()
                    content_keys[normalized] = key_value
                    self.log.info(f" + KEY: {normalized}:{key_value}")
            except Exception as e:
                self.log.warning(
                    f" - PlayReady key extraction failed for KID {kid}: {e}"
                )

        # set keys on all encrypted tracks so dl.py skips CDM.
        if content_keys:
            key_value = next(iter(content_keys.values()))
            for track in tracks:
                if hasattr(track, "encrypted") and track.encrypted:
                    track.key = key_value

    def license(self, challenge: bytes, title: Title, **kwargs):
        if not self.license_api:
            r = requests.post(
                url=self.config["endpoints"]["license"],
                headers=self._android_headers(),
                json={
                    "actionType": "play",
                    "assetId": str(title.service_data["metadata"]["contentId"]),
                    "browser": "ie" if self.playready else "chrome",
                    "deviceId": self.device_id,
                    "os": "android",
                    "platform": "web",
                },
            )
            try:
                lic_res = r.json()
            except Exception:
                lic_res = {}

            license_result = lic_res.get("resultObj") or {}
            license_url = license_result.get("laURL")
            license_token = license_result.get("token")
            if license_url and license_token:
                self.license_api = f"{license_url}?ExpressPlayToken={license_token}"
            elif license_url:
                self.license_api = license_url
            else:
                self.log.exit(" - Unable to obtain license URL.")

        return requests.post(
            url=self.license_api,
            data=challenge,
        ).content

    def configure(self):
        self.session.headers.update(
            {
                "User-Agent": self.android_user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": self.web_accept_language,
                "x-via-device": "true",
                "Origin": "https://www.sonyliv.com",
                "Referer": "https://www.sonyliv.com/",
            }
        )
        self.prepToken()

    def prepToken(self):
        cache_path = self.get_cache(
            "{profile}_tokens.json".format(profile=self.profile)
        )

        if os.path.isfile(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                tokens = json.load(f)
            self.accessToken = tokens["token"]
            self.session_id = tokens["session_id"]
            self.security_token = tokens["security_token"]
            self.device_id = tokens["device_id"]
            self.profile_id = tokens["profile_id"]
            self.log.info(" + Loaded tokens from cache.")
            return

        # OTP + Android TV activation flow thanks to Hugov
        self.log.info(" + No cached tokens found. Starting OTP login flow...")

        raw_mobile_number = input("Mobile number: ").strip()
        mobile_number = re.sub(r"\D+", "", raw_mobile_number or "")

        if not mobile_number or len(mobile_number) < 8:
            raise self.log.exit(" - Invalid mobile number.")

        web_device_id = f"{secrets.token_hex(16)}-{int(time.time() * 1000)}"
        web_session_id = f"{secrets.token_hex(16)}-{int(time.time() * 1000)}"
        web_ga_user_id = (
            f"GA1.2.{random.randint(100000000, 9999999999)}"
            f".{random.randint(1500000000, 1999999999)}"
        )

        web_session = requests.Session()
        android_session = requests.Session()

        web_headers = {
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua": self.web_sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "session_id": web_session_id,
            "User-Agent": self.web_user_agent,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "device_id": web_device_id,
            "x-via-device": "true",
            "Origin": "https://www.sonyliv.com",
            "Referer": "https://www.sonyliv.com/",
            "Accept-Language": self.web_accept_language,
        }

        try:
            web_session.get(
                "https://www.sonyliv.com/",
                headers={
                    "User-Agent": self.web_user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": self.web_accept_language,
                },
                timeout=30,
            )
        except Exception:
            pass

        try:
            web_session.get(
                "https://bootstrap-prod.sonyliv.com/AGL/1.0/WEB/ULD",
                headers={
                    "User-Agent": self.web_user_agent,
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://www.sonyliv.com",
                    "Referer": "https://www.sonyliv.com/",
                    "Accept-Language": self.web_accept_language,
                },
                timeout=30,
            )
        except Exception:
            pass

        self.log.info(" + Sending OTP...")

        otp_url = "https://apiv2.sonyliv.com/AGL/1.6/A/ENG/WEB/IN/BR/CREATEOTP-V2"

        create_otp_r = web_session.post(
            otp_url,
            headers=web_headers,
            json={
                "mobileNumber": mobile_number,
                "channelPartnerID": "MSMIND",
                "country": "IN",
                "timestamp": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "otpSize": 4,
                "loginType": "REGISTERORSIGNIN",
                "isMobileMandatory": True,
            },
            timeout=30,
        )

        try:
            create_otp_data = create_otp_r.json()
        except Exception:
            create_otp_data = {}

        if not create_otp_r.ok:
            raise self.log.exit(
                f" - OTP request failed: HTTP {create_otp_r.status_code}"
            )

        if (
            create_otp_data.get("resultCode")
            and create_otp_data.get("resultCode") != "OK"
        ):
            raise self.log.exit(
                f" - OTP request failed: {json.dumps(create_otp_data)[:300]}"
            )

        create_result_obj = create_otp_data.get("resultObj") or {}
        masked_mobile = create_result_obj.get("maskedMobileNumber") or mobile_number
        otp_delivery = create_result_obj.get("otpOn") or "SMS"

        self.log.info(f" + OTP sent via {otp_delivery} to {masked_mobile}")

        otp_code = ""
        while True:
            input_queue: queue.Queue = queue.Queue()
            thread = threading.Thread(
                target=lambda q: q.put(input("OTP code: ").strip()),
                args=(input_queue,),
                daemon=True,
            )
            thread.start()

            try:
                raw_otp = input_queue.get(timeout=60)
            except queue.Empty:
                self.log.info(" + OTP entry timed out. Resending OTP...")
                try:
                    web_session.post(
                        otp_url,
                        headers=web_headers,
                        json={
                            "mobileNumber": mobile_number,
                            "channelPartnerID": "MSMIND",
                            "country": "IN",
                            "timestamp": datetime.now(timezone.utc)
                            .isoformat(timespec="milliseconds")
                            .replace("+00:00", "Z"),
                            "otpSize": 4,
                            "loginType": "REGISTERORSIGNIN",
                            "isMobileMandatory": True,
                            "smsType": "SMS",
                        },
                        timeout=30,
                    )
                    self.log.info(" + OTP resent.")
                except Exception:
                    pass
                continue

            otp_code = re.sub(r"\D+", "", raw_otp or "")
            if not otp_code:
                self.log.warning(" - Empty OTP entered. Please try again.")
                continue
            break

        self.log.info(" + Verifying OTP...")

        confirm_r = web_session.post(
            "https://apiv2.sonyliv.com/AGL/2.4/A/ENG/WEB/IN/BR/CONFIRMOTP-V2",
            headers=web_headers,
            json={
                "channelPartnerID": "MSMIND",
                "mobileNumber": mobile_number,
                "country": "IN",
                "otp": otp_code,
                "dmaId": "IN",
                "ageConfirmation": True,
                "timestamp": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "isMobileMandatory": True,
                "deviceDetails": {
                    "deviceName": "NA",
                    "deviceType": "webClient",
                    "gaUserId": web_ga_user_id,
                    "modelNo": "Edge  Browser",
                    "serialNo": web_device_id,
                },
                "address": {"state": "BR"},
            },
            timeout=30,
        )

        try:
            confirm_data = confirm_r.json()
        except Exception:
            confirm_data = {}

        if not confirm_r.ok:
            raise self.log.exit(
                f" - OTP verification failed: HTTP {confirm_r.status_code}"
            )

        if confirm_data.get("resultCode") and confirm_data.get("resultCode") != "OK":
            raise self.log.exit(
                f" - OTP verification failed: {json.dumps(confirm_data)[:300]}"
            )

        confirm_result_obj = confirm_data.get("resultObj") or {}
        web_auth_token = confirm_result_obj.get("accessToken") or ""

        if not web_auth_token:
            raise self.log.exit(" - Web access token not found in OTP response.")

        self.log.info(" + OTP verified. Fetching web profile...")

        try:
            web_profile_r = web_session.get(
                "https://apiv2.sonyliv.com/AGL/2.9/A/ENG/WEB/IN/BR/GETPROFILE?channelPartnerID=MSMIND",
                headers={**web_headers, "Authorization": web_auth_token},
                timeout=30,
            )
            if web_profile_r.ok:
                web_profile_result = web_profile_r.json().get("resultObj") or {}
                web_auth_token = web_profile_result.get("accessToken") or web_auth_token
        except Exception:
            pass

        self.log.info(" + Starting Android TV device activation...")

        android_session_id = uuid.uuid4().hex
        android_device_id = uuid.uuid4().hex[:16]

        tv_headers = {
            "Content-Type": "application/json",
            "x-via-device": "true",
            "Host": "apiv2.sonyliv.com",
            "session-id": android_session_id,
            "user-agent": self.android_user_agent,
            "build_number": self.android_build_number,
            "app_version": self.android_app_version,
            "device_id": android_device_id,
        }

        sec_r = android_session.get(
            "https://apiv2.sonyliv.com/AGL/1.5/A/ENG/FIRE_TV/IN/GETTOKEN",
            headers={"Host": "apiv2.sonyliv.com", "user-agent": "okhttp/3.14.9"},
            timeout=30,
        )

        try:
            sec_data = sec_r.json()
        except Exception:
            sec_data = {}

        if not sec_r.ok:
            raise self.log.exit(
                f" - Security token request failed: HTTP {sec_r.status_code}"
            )

        security_token = sec_data.get("resultObj") or ""
        if not security_token:
            raise self.log.exit(" - Security token missing from response.")

        tv_headers["security_token"] = security_token

        # Get ULD (user location data)
        uld_r = android_session.get(
            "https://apiv2.sonyliv.com/AGL/1.5/A/ENG/FIRE_TV/IN/USER/ULD",
            headers=tv_headers,
            timeout=30,
        )

        try:
            uld_data = uld_r.json()
        except Exception:
            uld_data = {}

        if not uld_r.ok:
            raise self.log.exit(f" - ULD request failed: HTTP {uld_r.status_code}")

        uld_result = uld_data.get("resultObj") or {}
        state_code = uld_result.get("state_code") or "BR"
        city = uld_result.get("city") or "NA"
        channelpartnerid = uld_result.get("channelPartnerID") or "MSMIND"

        activation_payload = {
            "channelPartnerID": channelpartnerid,
            "deviceBrand": "Amazon",
            "deviceModelNumber": "AmazonAFTMM",
            "deviceName": "Fire TV Sony Liv",
            "deviceType": "FireTV",
            "location": city,
            "serialNo": android_device_id,
        }

        # Generate device activation code
        act_code_r = android_session.post(
            "https://apiv2.sonyliv.com/AGL/1.5/A/ENG/FIRE_TV/IN/GENERATEDEVICEACTIVATIONCODE",
            headers=tv_headers,
            json=activation_payload,
            timeout=30,
        )

        try:
            act_code_data = act_code_r.json()
        except Exception:
            act_code_data = {}

        if not act_code_r.ok:
            raise self.log.exit(
                f" - Activation code request failed: HTTP {act_code_r.status_code}"
            )

        act_code_result = act_code_data.get("resultObj") or {}
        activation_code = act_code_result.get("activationCode") or ""

        if not activation_code:
            raise self.log.exit(
                f" - Activation code missing: {json.dumps(act_code_data)[:300]}"
            )

        self.log.info(
            f" + Activation code: {activation_code}. Registering TV device with web account..."
        )

        # Register device using web token + activation code
        reg_r = web_session.post(
            "https://apiv2.sonyliv.com/AGL/1.6/SR/ENG/WEB/IN/REGISTERDEVICE",
            headers={**web_headers, "Authorization": web_auth_token},
            json={
                "channelPartnerID": "MSMIND",
                "activationCode": activation_code,
                "activationtype": "activationcode",
            },
            timeout=30,
        )

        try:
            reg_data = reg_r.json()
        except Exception:
            reg_data = {}

        if not reg_r.ok:
            raise self.log.exit(
                f" - Device registration failed: HTTP {reg_r.status_code}"
            )

        if reg_data.get("resultCode") and reg_data.get("resultCode") != "OK":
            raise self.log.exit(
                f" - Device registration failed: {json.dumps(reg_data)[:300]}"
            )

        self.log.info(" + TV device registered. Generating Android TV access token...")

        # call GENERATEDEVICEACTIVATIONCODE again to get accessToken
        token_r = android_session.post(
            "https://apiv2.sonyliv.com/AGL/1.5/A/ENG/FIRE_TV/IN/GENERATEDEVICEACTIVATIONCODE",
            headers=tv_headers,
            json=activation_payload,
            timeout=30,
        )

        try:
            token_data = token_r.json()
        except Exception:
            token_data = {}

        if not token_r.ok:
            raise self.log.exit(
                f" - Android TV token request failed: HTTP {token_r.status_code}"
            )

        token_result = token_data.get("resultObj") or {}
        token = token_result.get("accessToken") or ""

        if not token:
            raise self.log.exit(
                f" - Android TV access token missing: {json.dumps(token_data)[:300]}"
            )

        self.log.info(" + Got Android TV access token. Fetching TV profile...")

        profile_r = android_session.get(
            f"https://apiv2.sonyliv.com/AGL/3.3/A/ENG/FIRE_TV/IN/{state_code}/GETPROFILE"
            f"?channelPartnerID={channelpartnerid}",
            headers={**tv_headers, "Authorization": token},
            timeout=30,
        )

        try:
            profile_data = profile_r.json()
        except Exception:
            profile_data = {}

        if not profile_r.ok:
            raise self.log.exit(
                f" - TV profile request failed: HTTP {profile_r.status_code}"
            )

        profile_result = profile_data.get("resultObj") or {}
        contact_messages = profile_result.get("contactMessage") or []
        contact_id = ""

        if contact_messages and isinstance(contact_messages[0], dict):
            contact_id = str(contact_messages[0].get("contactID") or "")

        if not contact_id:
            raise self.log.exit(
                " - Profile contact ID missing from TV profile response."
            )

        tokens_to_save = {
            "token": token,
            "session_id": android_session_id,
            "security_token": security_token,
            "device_id": android_device_id,
            "profile_id": contact_id,
        }

        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(tokens_to_save, f, indent=2)

        self.accessToken = token
        self.session_id = android_session_id
        self.security_token = security_token
        self.device_id = android_device_id
        self.profile_id = contact_id

        self.log.info(" + Authentication complete. Tokens saved to cache.")
