import base64
import hashlib
import hmac
import json
import os
import time
import uuid
import click
import m3u8
import secrets
import json
import uuid
import re
import requests
import re
from curl_cffi import requests as requests2

from vinetrimmer.config import directories
from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class Hotstar(BaseService):
    """
    Service code for Star India's Hotstar (aka Disney+ Hotstar) streaming service (https://hotstar.com).

    \b
    Authorization: Credentials
    Security: UHD@L3, doesn't seem to care about releases.

    \b
    Tips: - The library of contents can be viewed without logging in at https://hotstar.com
          - The homepage hosts domestic programming; Disney+ content is at https://hotstar.com/in/disneyplus
    """

    ALIASES = ["JHS", "Hotstar"]
    TITLE_RE = r"^(?:https?://(?:www\.)?hotstar\.com/[a-z0-9/-]+/)(?P<id>\d+)"

    @staticmethod
    @click.command(name="Hotstar", short_help="https://hotstar.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Hotstar(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        
        self.original_url = title
        self.parse_title(ctx, title)
        
        if "/movies/" in title:
            self.movie = True
        else:
            self.movie = False

        assert ctx.parent is not None
        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"] or "EC3"
        self.range = ctx.parent.params["range_"]
        
        self.hdrdv = ctx.parent.params.get("hdrdv", False)
        if self.hdrdv:
            self.log.info(" + HDR+DV mode enabled")

        self.profile = ctx.obj.profile

        self.device_id = None
        self.hotstar_auth = None
        self.hdntl = None
        self.userUP = None
        self.license_api = None
        self.token = None
        self.region = "in"
        self.content_type = None
        self.lang = "und"

        self.configure()

    def get_titles(self):
        url = self.config["endpoints"]["movie_title"]
        params = {
            "content_id": self.title,
            "client_capabilities": json.dumps({
                "package": ["dash", "hls"],
                "container": ["fmp4", "ts", "fmp4br"],
                "ads": ["non_ssai", "ssai"],
                "audio_channel": ["dolbyatmos","atmos","dolby51","stereo"],
                "encryption": ["plain", "widevine"],
                "ladder": ["tv"],
                "resolution": ["4k"]
            }),
            "drm_parameters": json.dumps({
                "widevine_security_level": [
                    "SW_SECURE_DECODE",
                    "SW_SECURE_CRYPTO"
                ],
                "hdcp_version": ["HDCP_NONE"]
            })
        }
        max_retries = 3
        for attempt in range(max_retries):
            try:
                res = self.session.get(
                    url,
                    params=params
                )
                break
            except (requests.exceptions.SSLError, requests.exceptions.ProxyError, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                else:
                    raise
            
        if res.status_code != 200:
            try:
                error_data = res.json()
                self.log.exit(f"Error Occurred: {json.dumps(error_data)}")
            except:
                self.log.exit(f"Error Occurred: Status {res.status_code}, {res.text[:200]}")
        
        try:
            data = res.json()["success"]["page"]["spaces"]["spotlight"]["widget_wrappers"][0]["widget"]["data"]["content_info"]
        except KeyError:
            self.log.error(f" + Response body: {res.json().get('body', res.json())}")
            raise ValueError(f"Failed to get title data - 'results' key not found in body")
            # Handle language - different endpoints return different formats
        lang_data = data["content_language_selector"].get("languages")
            
        self.lang = "eng"

        for lang in lang_data:
            if lang.get("description") == "Original":
                org_lang = lang["language"]["iso3code"]
                break
        # Handle Movie
        if not "category_picker" in res.json()["success"]["page"]["spaces"]["tabbed"]["widget_wrappers"][0]["widget"]["data"]:
            self.content_type = "MOVIE"
            
            return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=data["title"],
                original_lang=self.lang,
                year=data["core_meta_tags"][0]["value"],
                source=self.ALIASES[0],
                service_data=data
            )
        # Handle TV Shows
        episodes = res.json()["success"]["page"]["spaces"]["tabbed"]["widget_wrappers"][0]["widget"]["data"]["category_picker"]["data"]["tabs"]
        episode_details2 = []
        result = []
        for episode in episodes:
            tray_widget_url = "https://apix.hotstar.com" + episode["tab"]["data"]["tray_widget_url"]
            response3 = self.session.get(tray_widget_url).json()
            widget_data = response3["success"]["widget_wrapper"]["widget"]["data"]
            content_data = response3["success"]["widget_wrapper"]["widget"]["data"][ "items"]
            for e in content_data:
                content_data = e["playable_content"]["data"]
                episode_title = content_data.get("title").replace("/",".").replace("!",".")
                episode_id = content_data["content_id"]
                tags = content_data.get("tags", [])
                episode_tag = next(
                    (tag["value"].replace(' ','.') for tag in tags if "S" in tag["value"]),
                    ""
                )
                season = None
                episode = None

                if episode_tag:
                    m = re.search(r"S(\d+)[\.]?E(\d+)", episode_tag, re.I)
                    if m:
                        season = int(m.group(1))
                        episode = int(m.group(2))
                year_tag = str(tags[1]["value"])
                year = str(year_tag.split()[-1])
                result.append({
                    'id': episode_id,
                    "name": data["title"],
                    'year': year,
                    "season": season,
                    "episode": episode,
                    "episode_title": episode_title
                })   
            next_tray_url = widget_data.get("next_tray_url")
            if next_tray_url:
                # ---- Loop next pages ----
                while next_tray_url:
                    url = "https://apix.hotstar.com" + next_tray_url
                    response2 = self.session.get(url).json()
                    widget_data = response2["success"]["widget_wrapper"]["widget"]["data"]
                    extra_episode = response2["success"]["widget_wrapper"]["widget"]["data"][ "items"]
                    for ep in extra_episode:
                        content_data = ep["playable_content"]["data"]
                        episode_title = content_data.get("title")
                        episode_id = content_data["content_id"]
                        tags = content_data.get("tags", [])
                        episode_tag = next((tag["value"].replace(' ','.') for tag in tags if "S" in tag["value"]), None)
                        season = None
                        episode = None

                        if episode_tag:
                            m = re.search(r"S(\d+)[\.]?E(\d+)", episode_tag, re.I)
                            if m:
                                season = int(m.group(1))
                                episode = int(m.group(2))
                        year_tag = str(tags[1]["value"])
                        year = year_tag.split()[-1]
                        result.append({
                            'id': episode_id,
                            "name": data["title"],
                            'year': year,
                            "season": season,
                            "episode": episode,
                            "episode_title": episode_title
                        })
                    next_tray_url = widget_data.get("next_tray_url")
        
        return [
            Title(
                id_=x.get("id"),
                type_=Title.Types.TV,
                name=x.get("showShortTitle", x.get("name")),
                year=x.get("year"),
                season=x.get("season"),
                episode=x.get("episode"),
                episode_name=x.get("episode_title"),
                source=self.ALIASES[0],
                original_lang=self.lang,
                service_data=x
            ) for x in result
        ]
    

    def get_tracks(self, title):
        if hasattr(self, 'hdrdv') and self.hdrdv:
            self.log.info("HDR+DV Mode: Fetching both HDR10 and DV manifests...")
            tracks = Tracks()
            
            self.log.info(" + Fetching HDR10 manifest...")
            hdr10_tracks = self._fetch_manifest(title, "HDR10")
            if hdr10_tracks and hdr10_tracks.videos:
                self.log.info(f" + Found {len(hdr10_tracks.videos)} HDR10 video tracks")
                tracks.videos.extend(hdr10_tracks.videos)
                if not tracks.audios:
                    tracks.audios.extend(hdr10_tracks.audios)
                if not tracks.subtitles:
                    tracks.subtitles.extend(hdr10_tracks.subtitles)
            
            self.log.info(" + Fetching DV manifest...")
            dv_tracks = self._fetch_manifest(title, "DV")
            if dv_tracks and dv_tracks.videos:
                self.log.info(f" + Found {len(dv_tracks.videos)} DV video tracks")
                tracks.videos.extend(dv_tracks.videos)
                if not tracks.audios:
                    tracks.audios.extend(dv_tracks.audios)
                if not tracks.subtitles:
                    tracks.subtitles.extend(dv_tracks.subtitles)
            else:
                self.log.info(" + No DV tracks found")
            
            return tracks
        else:
            return self._fetch_manifest(title, self.range)
    
    def _fetch_manifest(self, title, range_override=None):
        current_range = range_override or self.range
        
        if current_range != "SDR" and self.vcodec != "H265":
            self.vcodec = "H265"
            if not range_override:
                self.log.info(f" + Switched Video Codec to H265 to be able to get {current_range} Dynamic Range")

        if self.vcodec == "H265":
            if current_range == "SDR":
                range_val = "sdr"
                vcodec = "h265"
            elif current_range == "HDR10":
                range_val = "hdr10"
                vcodec = "h265"
            elif current_range == "DV":
                range_val = "dv"
                vcodec = "dvh265"
            else:
                range_val = "sdr"
                vcodec = "h265"
        else:
            range_val = "sdr"
            vcodec = "h264"
        
        params = {
            "content_id": self.title,
            "client_capabilities": json.dumps({
                "package": ["dash", "hls"],
                "container": ["fmp4", "ts", "fmp4br"],
                "ads": ["non_ssai", "ssai"],
                "audio_channel": ["dolbyatmos","atmos","dolby51","stereo"],
                "encryption": ["plain", "widevine"],
                "video_codec": [f"{vcodec.lower()}"],
                "ladder": ["tv"],
                "resolution": ["4k"],
                "dynamic_range": [f"{range_val}"]
            }),
            "drm_parameters": json.dumps({
                "widevine_security_level": [
                    "SW_SECURE_DECODE",
                    "SW_SECURE_CRYPTO"
                ],
                "hdcp_version": ["HDCP_NONE"]
            }),
            "request_features": "consent_supported"
        }
        watch_url = self.config["endpoints"].get("watch", "https://apix.hotstar.com/v2/pages/watch")
        try:
            res = self.session.get(
                url=watch_url,
                params=params,
                timeout=45,
                verify=False
            )
            
            playback_set = res.json()["success"]["page"]["spaces"]["player"]["widget_wrappers"][0]["widget"]["data"]["player_config"]["media_asset"]["primary"]["content_url"]
            cdn_domain = playback_set.split('/')[2] if '/' in playback_set else playback_set
            self.log.debug(f" + CDN: {cdn_domain}")
            
            license_url = None
            try:
                licence_urls = res.json()["success"]["page"]["spaces"]["player"]["widget_wrappers"][0]["widget"]["data"]["player_config"]["media_asset"]["licence_urls"]
                if licence_urls is not None:
                    license_url = licence_urls[0]
                    if not hasattr(self, 'license_api') or self.license_api is None:
                        self.license_api = license_url
            except:
                pass
                
        except (KeyError, TypeError) as e:
            self.log.exit(f"Manifest fetch failed: {res.text}")
        except (requests.exceptions.SSLError, requests.exceptions.ProxyError, requests.exceptions.ConnectionError) as e:
            self.log.exit(f" - Playback request failed. Try different proxy server.")
        manifest_headers = {
            "User-Agent": self.token_data["User-Agent"],
            "X-Hs-Device-Id": self.device_id,
            "X-HS-UserToken": self.userUP,
            "hotstarauth": self.hotstar_auth,
        }
        proxy = self.session.proxies.get("all")
        self.log.info(f" + Manifest URL: {playback_set}...")
        session = requests2.Session(
            impersonate="chrome",   # chrome / chrome110 / firefox / safari
        )
        manifest_res = session.get(
            playback_set,
            headers=self.session.headers,
            proxies={
                "http": proxy,
                "https": proxy
            },
            verify=False
        )
        print(manifest_res.status_code)
        if manifest_res.status_code != 200:
            raise Exception(f"HTTP {manifest_res.status_code}: {manifest_res.text[:200]}")
            
        mpd_cookie = manifest_res.cookies.get_dict()
        cookie_header = ";".join([f"{k}={v}" for k, v in mpd_cookie.items()]) + ";"
            
        manifest_data = manifest_res.text
        new_url = playback_set
            
        self.log.info(f" + Cookies: {cookie_header[:200]}")
        if "m3u8" in new_url:
            tracks = Tracks.from_m3u8(
                m3u8.loads(manifest_data, new_url),
                lang=title.original_lang,
                source=self.ALIASES[0]
            )
            for track in tracks:
                track.encrypted = False
        else:
            tracks = Tracks.from_mpd(
                url=new_url,
                data=manifest_data,
                session=self.session,
                source=self.ALIASES[0]
            )

        cookies_dict = {}
        if "CloudFront-Key-Pair-Id" in mpd_cookie:
            cookies_dict["CloudFront-Key-Pair-Id"] = mpd_cookie["CloudFront-Key-Pair-Id"]
        if "CloudFront-Policy" in mpd_cookie:
            cookies_dict["CloudFront-Policy"] = mpd_cookie["CloudFront-Policy"]
        if "CloudFront-Signature" in mpd_cookie:
            cookies_dict["CloudFront-Signature"] = mpd_cookie["CloudFront-Signature"]
        if "hdntl" in mpd_cookie:
            cookies_dict["hdntl"] = mpd_cookie["hdntl"]

        cookie = '; '.join([f'{name}={value}' for name, value in cookies_dict.items()])

        self.session.headers.update({
            'cookie': cookie,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-us,en;q=0.5",
            "Referer": "https://www.hotstar.com/in",
        })

        from vinetrimmer.utils.n_m3u8dl_config import store_manifest_url
        store_manifest_url(tracks, new_url, self.ALIASES[0])
        
        for track in tracks:
            track.needs_proxy = True
            if license_url is None:
                track.encrypted = False

        for track in tracks.videos:
            if current_range == "HDR10":
                track.hdr10 = True
                track.dv = False
            elif current_range == "DV":
                track.dv = True
                track.hdr10 = False
            else:
                track.hdr10 = False
                track.hlg = False
                track.dv = False
            
            if track.language == "und":
                track.language = title.original_lang

        for track in tracks.audios:
            if track.language == "und":
                track.language = title.original_lang

        for subtitle in tracks.subtitles:
            if subtitle.language.language == "en":
                subtitle.sdh = True
            
            original_download = subtitle.download
            subtitle_url = subtitle.url
            session = self.session
            log = self.log
            
            def hotstar_subtitle_download(out, name=None, headers=None, proxy=None):
                import os
                os.makedirs(out, exist_ok=True)
                
                name = (name or "{type}_{id}_{enc}").format(
                    type="TextTrack",
                    id=subtitle.id,
                    enc="dec"
                ) + ".mp4"
                save_path = os.path.join(out, name)
                
                try:
                    response = session.get(subtitle_url, timeout=10, verify=False)
                    response.raise_for_status()
                    
                    with open(save_path, 'wb') as f:
                        f.write(response.content)
                    
                    subtitle._location = save_path
                    return save_path
                except Exception as e:
                    log.warning(f" + Subtitle download failed: {e}, trying fallback")
                    return original_download(out, name, headers, proxy)
            
            subtitle.download = hotstar_subtitle_download

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, **_):
        session = requests2.Session(
            impersonate="chrome",   # chrome / chrome110 / firefox / safari
        )

        proxy = self.session.proxies.get("all")

        proxies = {
            "http": proxy,
            "https": proxy
        }
        headers = {
            "Accept-Encoding": self.token["Accept-Encoding"],
            "Accept-Language": self.token["Accept-Language"],
            "app_name": self.token["app_name"],
            "Connection": self.token["Connection"],
            "Content-Type": "application/octet-stream",
            "User-Agent": self.token["User-Agent"],
            "X-HS-Accept-Language": self.token["Accept-Language"],
            "X-HS-App": self.token["X-HS-App"],
            "X-HS-Client": self.token["X-HS-Client"],
            "X-HS-Client-Targeting": self.token["X-HS-Client-Targeting"],
            "X-HS-Device-Id": self.device_id,
            "X-HS-Platform": self.token["X-HS-Platform"],
            "X-HS-Schema-Version": self.token["X-HS-Schema-Version"]
        }
        return session.post(url=self.license_api, data=challenge, headers=headers, proxies=proxies).content

    def configure(self):
        self.session.headers.update({
            "Origin": "https://www.hotstar.com",
            "Referer": f"https://www.hotstar.com/{self.region}",
        })
        
        self.log.info("Logging into Hotstar")
        token_data = self.get_token()
        self.token = token_data
        self.log.info(f" + Using Device ID: {self.device_id}")
        self.log.info(" + Obtained tokens")
        
    def get_token(self):
        token_cache_path = self.get_cache(f"token_{self.profile}.json")
        if os.path.isfile(token_cache_path):
            with open(token_cache_path, encoding="utf-8") as fd:
                token = json.load(fd)
                self.session.headers = {
                    "User-Agent": token["User-Agent"],
                    "Accept-Encoding": token["Accept-Encoding"],
                    "Content-Type": token["Content-Type"],
                    "Accept": "*/*",
                    "Connection": token["Connection"],
                    "Accept-Language": token["Accept-Language"],
                    "app_name": token["app_name"],
                    "X-HS-Accept-Language": token["X-HS-Accept-Language"],
                    "X-HS-App": token["X-HS-App"],
                    "X-HS-Client": token["X-HS-Client"],
                    "X-HS-Client-Targeting": token["X-HS-Client-Targeting"],
                    "X-HS-Device-Id": token["X-HS-Device-Id"],
                    "X-HS-Platform": token["X-HS-Platform"],
                    "X-HS-Schema-Version":token["X-HS-Schema-Version"],
                    "X-HS-Is-Retry": token["X-HS-Is-Retry"],
                    "X-Hs-ProxyState-ud": token["X-Hs-ProxyState-ud"],
                    "X-Hs-UserToken": token["X-Hs-UserToken"],
                    "X-Hs-ProxyState": token["X-Hs-ProxyState"]
                }
            if True:
                # not expired, lets use
                self.log.info(" + Using cached auth tokens...")
                return token
        # get new token
        self.session.headers = self.login()
        config = {}
        for key, value in self.session.headers.items():
            config[key] = value
        os.makedirs(os.path.dirname(token_cache_path), exist_ok=True)
        with open(token_cache_path, "w", encoding="utf-8") as fd:
            json.dump(config, fd, indent=4, ensure_ascii=False)
        with open(token_cache_path, encoding="utf-8") as fd:
            token = json.load(fd)
        return token

    def get_x_hs_guest_token(self):
        self.device_id = secrets.token_hex(16 // 2)
        headers = {
            "Accept-Encoding": "gzip",
            "Accept-Language": "eng",
            "app_name": "android",
            "Connection": "Keep-Alive",
            "User-Agent": "Hotstar;in.startv.hotstar/26.06.08.6.4557 (Android/12)",
            # Hotstar headers
            "X-HS-Accept-Language": "eng",
            "X-HS-App": "4557",
            # 🔁 Client changed to Sony Android TV (STRUCTURE)
            "X-HS-Client": (
                "platform:androidtv;"
                "app_id:in.startv.hotstar;"
                "app_version:26.06.08.6;"
                "os:Android;"
                "os_version:12;"
                "schema_version:0.0.1705;"
                "brand:Sony;"
                "model:BRAVIA_4K_GB"
            ),
            "X-HS-Client-Targeting": f"ad_id:;user_lat:false;hw_id:{self.device_id}",
            "X-HS-Device-Id": self.device_id,
            "X-HS-Platform": "androidtv",
            "X-HS-Schema-Version": "0.0.1705",
        }
        headers["Content-Type"] = "application/json"
        params = {
            "client_capabilities": json.dumps({
                "package": ["dash", "hls"],
                "container": ["fmp4", "ts", "fmp4br"],
                "ads": ["non_ssai", "ssai"],
                "audio_channel": ["dolbyatmos","atmos","dolby51","stereo"],
                "encryption": ["plain", "widevine"],
                "video_codec": ["h264"],
                "ladder": ["phone", "tv"],
                "resolution": ["sd", "hd", "fhd", "4k"],
                "dynamic_range": ["SDR"]
            }),
            "drm_parameters": json.dumps({
                "widevine_security_level": [
                    "SW_SECURE_DECODE",
                    "SW_SECURE_CRYPTO"
                ],
                "hdcp_version": ["HDCP_NONE"]
            })
        }
        self.session.headers.clear()
        self.session.headers = headers
        proxy_sate = self.session.get("https://usersvc.hotstar.com/v1/location").headers.get("x-hs-setproxystate-ud")
        url = "https://apix.hotstar.com/v2/freshstart"
        self.session.headers.update({
            "X-HS-Is-Retry": "false",
            "X-Hs-ProxyState-ud": proxy_sate
        })
        payload = f'''
        {{
          "2":"",
          "6":1,
          "7":{{
              "1": {{
                  "1":"{self.device_id}",
                  "2":3
              }},
              "2": {{
                  "2":"Android",
                  "3":"31"
              }}
          }}
        }}
        '''.encode()
        response = self.session.post(
            url,
            params=params,
            data=payload
        )
        x_hs_hid = response.headers.get("x-hs-hid")
        x_hs_pid = response.headers.get("x-hs-pid")
        x_hs_request_id = response.headers.get("x-hs-request-id")
        x_hs_updated_user_info = response.headers.get("x-hs-updated-user-info")
        x_hs_updated_user_token_1 = response.headers.get("x-hs-updatedusertoken")
        x_hs_user_login_state = response.headers.get("x-hs-user-login-state")
        x_hs_request_id = response.headers.get("x-request-id")
        payload = {
            "2": "",
            "6": 2
        }
        params = {
            "action": "CONSENT_SAVED",
            "sm_ctx": json.dumps({
                "DomainContext": {
                    "IsNewUser": False,
                    "IsDefaultProfileCreated": False,
                    "IsEmailCaptureSkipped": False,
                    "IsSkipLogin": False,
                    "AppLaunchCounter": 0,
                    "LoginStatus": "",
                    "SilentLoginType": "",
                    "EmailCaptureSourceType": "",
                    "IsProfileUpdateRequired": False,
                    "IsEarlyPaywallShown": False
                },
                "BaseContext": {
                    "StateIdentifier": "CONSENT_STATE",
                    "StateMachineIdentifier":
                    "ONBOARDING_STATE_MACHINE"
                },
                "ActionContext": None
            }, separators=(",", ":")),
            "client_capabilities": json.dumps({
                "package": ["dash", "hls"],
                "container": ["fmp4", "ts", "fmp4br"],
                "ads": ["non_ssai", "ssai"],
                "audio_channel": ["dolbyatmos","atmos","dolby51","stereo"],
                "encryption": ["plain", "widevine"],
                "video_codec": ["h264"],
                "ladder": ["phone", "tv"],
                "resolution": ["sd", "hd", "fhd", "4k"],
                "dynamic_range": ["SDR"]
            }, separators=(",", ":")),
            "drm_parameters": json.dumps({
                "widevine_security_level":[
                    "SW_SECURE_DECODE",
                    "SW_SECURE_CRYPTO"
                ],
                "hdcp_version":["HDCP_NONE"]
            }, separators=(",", ":"))
        }
        self.session.headers.update({
            "X-HS-Client-Targeting": f"ad_id:571460f7-a9b5-4985-9701-dc68fe94f7ff;user_lat:false;hw_id:{self.device_id}",
            "Host": "apix.hotstar.com",
            "X-Hs-UserToken": x_hs_updated_user_token_1
        })
        response = self.session.post(
            url,
            params=params,
            data=json.dumps(
                payload,
                separators=(",", ":")
            ).encode()
        )
        x_hs_updated_user_token = response.headers.get("x-hs-updatedusertoken")
        if not x_hs_updated_user_token:
            x_hs_updated_user_token = x_hs_updated_user_token_1
        self.session.headers.update({
            "X-HS-Client-Targeting": f"ad_id:571460f7-a9b5-4985-9701-dc68fe94f7ff;user_lat:false;hw_id:{self.device_id}",
            "Host": "apix.hotstar.com",
            "X-Hs-UserToken": x_hs_updated_user_token
        })
        return self.session

    def login(self):
        params = {
            "client_capabilities": json.dumps({
                "package": ["dash", "hls"],
                "container": ["fmp4", "ts", "fmp4br"],
                "ads": ["non_ssai", "ssai"],
                "audio_channel": ["dolbyatmos","atmos","dolby51","stereo"],
                "encryption": ["plain", "widevine"],
                "video_codec": ["h264"],
                "video_codec_non_secure": ["h264", "h265", "vp9"],
                "ladder": ["phone", "tv"],
                "resolution": ["sd", "hd", "fhd", "4k"],
                "dynamic_range": ["SDR"]
            }),
            "drm_parameters": json.dumps({
                "widevine_security_level": [
                    "SW_SECURE_DECODE",
                    "SW_SECURE_CRYPTO"
                ],
                "hdcp_version": ["HDCP_NONE"]
            })
        }
        self.get_x_hs_guest_token()
        url = "https://apix.hotstar.com/v2/start"
        payload = b'''
        {
          "2": "",
          "6": 2
        }
        '''
        params["action"] = "CONSENT_SAVED"
        response = self.session.post(
            url,
            params=params,
            data=payload
        )
        self.session.headers.update({
            "X-Hs-ProxyState": response.headers.get("x-hs-setproxystate")
        })
        qr_url = "https://apix.hotstar.com" + response.json()["success"]["page"]["spaces"]["content"]["widget_wrappers"][0]["widget"]["data"]["widgets"][0]["hero_widget"]["widget_commons"]["data_bind_mechanism"]["centralStore"]["http_request_commons"]["url"]
        src = response.json()["success"]["page"]["spaces"]["content"]["widget_wrappers"][0]["widget"]["data"]["widgets"][0]["hero_widget"]["data"]["illustration"]["image"]["src"]
        total_attempts = 300
        interval = 1  # seconds
        self.log.info(
            f"LOGIN_REQUIRED|{src}|0|{total_attempts * interval}"
        )
        print(f"LOGIN_REQUIRED|{src}|0|{total_attempts * interval}")
        params = {
            "client_capabilities": json.dumps({
                "package": ["dash", "hls"],
                "container": ["fmp4", "ts", "fmp4br"],
                "ads": ["non_ssai", "ssai"],
                "audio_channel": ["dolbyatmos","atmos","dolby51","stereo"],
                "encryption": ["plain", "widevine"],
                "video_codec": ["h264"],
                "ladder": ["tv"],
                "resolution": ["4k"],
                "dynamic_range": ["SDR"]
            }),
            "drm_parameters": json.dumps({
                "widevine_security_level": [
                    "SW_SECURE_DECODE",
                    "SW_SECURE_CRYPTO"
                ],
                "hdcp_version": ["HDCP_NONE"]
            })
        }
        payload = b'''
        {
          "1": ""
        }
        '''
        total_attempts = 300
        interval = 1  # seconds
        # ✅ polling loop (USE CORRECT API HERE)
        for i in range(total_attempts):
            try:
                response = self.session.post(qr_url, data=payload)
                login_state = response.headers.get("x-hs-user-login-state")
                if login_state == "LOGGEDIN":
                    updated_user_token = response.headers.get("x-hs-updatedusertoken")
                    if updated_user_token:
                        self.session.headers.update({
                            "X-Hs-UserToken": updated_user_token
                        })
                        self.log.info("LOGIN_SUCCESS")
                        print(json.dumps(dict(self.session.headers), indent=4))
                        return self.session.headers
                    else:
                        self.log.info("LOGIN state received, but token header missing")
                        return None
                else:
                    self.log.info(f"Not logged in yet (state={login_state})")
            except Exception as e:
                self.log.info(f"LOGIN_TIMEOUT|{str(e)}")

            time.sleep(interval)

        # ⛔ TIMEOUT
        self.log.info("LOGIN_TIMEOUT")
        return None