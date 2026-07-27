import json
import os
import re
import uuid
import requests
import click
import m3u8
import time
import uuid
import hashlib
from hashlib import md5

from vinetrimmer.objects import TextTrack, Title, Tracks
from vinetrimmer.services.BaseService import BaseService

# ANSI Color codes for enhanced logging
class Colors:
    CYAN = '\033[96m'
    YELLOW = '\033[93m'
    GREEN = '\033[92m'
    RESET = '\033[0m'


class SonyLiv(BaseService):

    ALIASES = ["SONY", "SonyLiv"]
    #GEOFENCE = ["hs"]

    @staticmethod
    @click.command(name="SonyLiv", short_help="https://sonyliv.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return SonyLiv(ctx, **kwargs)


    def __init__(self, ctx, title):
        super().__init__(ctx)
        title = title.replace("?watch=true", "")
        #self.log.info(title)
        self.title = title.split("-")[-1]
        parts = title.split("/")
        for part in reversed(parts):
            if part.isdigit():
                self.title = part
            else:
                sub_parts = part.split("-")
                for sub_part in reversed(sub_parts):
                    if sub_part.isdigit():
                        self.title = sub_part
        #self.log.info(self.title)

        assert ctx.parent is not None

        self.vcodec = ctx.parent.params["vcodec"]
        self.quality = ctx.parent.params["quality"]
        self.acodec = ctx.parent.params["acodec"] or "EC3"
        self.range = ctx.parent.params["range_"]
        self.hdrdv = ctx.parent.params["hdrdv"]

        self.profile = ctx.obj.profile

        self.device_id = None
        self.token = None
        self.license_api = None

        self.configure()


    def get_titles(self):
        r = self.session.get(
            url=self.config["endpoints"]["detail_url"].format(state_code=self.state_code, media_id=self.title),
            params={
                "kids_safe": "false",
                "from": "0",
                "to": "0",
                "segment_id": "AB_Free_Text_Tray_No_Tag,AB_New_Trays,AB_New_Trays_Home_Page,AB_TKMOC_Tray,AB_DetailPage_Disable",
                "revamp": "true",
                "onlymeta": "true",
            },
            headers = {
                "Accept-Encoding": "gzip",
                "app_version": self.config["device"]["app_version"],
                "build_number": self.config["device"]["build_number"],
                "Connection": "Keep-Alive",
                "Content-Type": "application/json",
                "device_id": self.device_id,
                "session_id": self.session_id,
                "user-agent": self.config["device"]["user_agent"],
                "x-via-device": "true",
            }
        try:
            res = r.json()['resultObj']['containers'][0]
            #self.log.info(res)
        except json.JSONDecodeError:
            print(r.status_code)
            raise ValueError(f"Failed to load title manifest: {r.text}")
        if res['metadata']['contentSubtype'] == 'MOVIE' or res['metadata']['contentSubtype'] == 'MOVIE_BUNDLE':
            # Extract duration from metadata if available
            duration = (res['metadata'].get('duration') or 
                       res['metadata'].get('runtime') or 
                       res['metadata'].get('length') or
                       res['metadata'].get('totalDuration') or
                       res['metadata'].get('runTime') or
                       res['metadata'].get('durationInSeconds') or
                       res['metadata'].get('durationInMinutes'))
            if duration:
                # Convert to seconds if needed (assuming it might be in minutes or already in seconds)
                if isinstance(duration, str) and ':' in duration:
                    # Format like "HH:MM:SS" or "MM:SS"
                    time_parts = duration.split(':')
                    if len(time_parts) == 3:
                        duration = int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])
                    elif len(time_parts) == 2:
                        duration = int(time_parts[0]) * 60 + int(time_parts[1])
                elif isinstance(duration, (int, float)):
                    # Check if this is from durationInMinutes field
                    if 'durationInMinutes' in res['metadata'] and res['metadata']['durationInMinutes'] == duration:
                        duration = int(duration * 60)  # Convert minutes to seconds
                    elif duration < 1000:  # Likely in minutes if less than 1000
                        duration = int(duration * 60)
                    else:
                        duration = int(duration)
                else:
                    duration = None
            if res["layout"] == "BUNDLE_ITEM":
                action_url = self.config["endpoints"]["action_url"].format(state_code=self.state_code) + res["actions"][0]["uri"]
                res2 = self.session.get(action_url).json()
                for i in res2["resultObj"]["containers"][0]["containers"]:
                    if i["metadata"]["objectSubtype"] == "MOVIE":
                        ids = i["metadata"]["contentId"]
            else:
                ids = res['metadata']['contentId']
            return Title(
                id_=ids,
                type_=Title.Types.MOVIE,
                name=re.sub(r'\([^)]*\)', '', res['metadata']["title"]),
                year=res['metadata']["year"],
                original_lang=res['metadata']["language"],
                source=self.ALIASES[0],
                service_data=res,
                duration=duration,
            )
        else:
            season_data = []
            info_dict = {}
            if res['metadata']['contentSubtype'] == "EPISODIC_SHOW":
                r = self.session.get(
                    url=self.config["endpoints"]["bundle_url"].format(state_code=self.state_code, media_id=res["id"]),
                    headers = {
                        "Accept-Encoding": "gzip",
                        "app_version": self.config["device"]["app_version"],
                        "Authorization": self.token  # Paste the full JWT here
                        "build_number": self.config["device"]["build_number"],
                        "Content-Type": "application/json",
                        "device_id": self.device_id,
                        "session_id": self.session_id,
                        "User-Agent": self.config["device"]["user_agent"],
                        "x-via-device": "true",
                    },
                    params={
                        "fromSeq": "0",
                        "toSeq": "1000",
                        "orderBy": "episode_series_sequence",
                        "sortOrder": "asc",
                        "kids_safe": "false",
                    },
                ).json()
                #self.log.info(r)

                for y in r['resultObj']['containers'][0]['containers']:
                    # Extract duration from episode metadata if available
                    duration = (y['metadata'].get('duration') or 
                               y['metadata'].get('runtime') or 
                               y['metadata'].get('length') or
                               y['metadata'].get('totalDuration') or
                               y['metadata'].get('runTime') or
                               y['metadata'].get('durationInSeconds') or
                               y['metadata'].get('durationInMinutes'))
                    if duration:
                        # Convert to seconds if needed
                        if isinstance(duration, str) and ':' in duration:
                            time_parts = duration.split(':')
                            if len(time_parts) == 3:
                                duration = int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])
                            elif len(time_parts) == 2:
                                duration = int(time_parts[0]) * 60 + int(time_parts[1])
                        elif isinstance(duration, (int, float)):
                            # Check if this is from durationInMinutes field
                            if 'durationInMinutes' in y['metadata'] and y['metadata']['durationInMinutes'] == duration:
                                duration = int(duration * 60)  # Convert minutes to seconds
                            elif duration < 1000:  # Likely in minutes
                                duration = int(duration * 60)
                            else:
                                duration = int(duration)
                        else:
                            duration = None
                    
                    info_dict = {
                        'id': y['metadata']['contentId'],
                        'name': res['metadata']["title"],
                        #'year': y['metadata']['year'],
                        'season': res['metadata']['season'],
                        'episode': y['metadata']['episodeNumber'],
                        'episodename': y['metadata']['episodeTitle'],
                        'originallang': y['metadata']['language'],
                        'servicedata': y,
                        'duration': duration,
                    }
                    season_data.append(info_dict)
            else:
                for x in res['containers']:

                    r = self.session.get(
                        url=self.config["endpoints"]["bundle_url"].format(state_code=self.state_code, media_id=x["id"]),
                        headers = {
                            "Accept-Encoding": "gzip",
                            "app_version": self.config["device"]["app_version"],
                            "Authorization": self.token  # Paste the full JWT here
                            "build_number": self.config["device"]["build_number"],
                            "Content-Type": "application/json",
                            "device_id": self.device_id,
                            "session_id": self.session_id,
                            "User-Agent": self.config["device"]["user_agent"],
                            "x-via-device": "true",
                        },
                        params={
                            "from": "0",
                            "to": "1000",
                            "orderBy": "episodeNumber",
                            "sortOrder": "asc",
                            "kids_safe": "false",
                        },
                    ).json()
                    #self.log.info(r)

                    for y in r['resultObj']['containers'][0]['containers']:
                        # Extract duration from episode metadata if available
                        duration = (y['metadata'].get('duration') or 
                                   y['metadata'].get('runtime') or 
                                   y['metadata'].get('length') or
                                   y['metadata'].get('totalDuration') or
                                   y['metadata'].get('runTime') or
                                   y['metadata'].get('durationInSeconds') or
                                   y['metadata'].get('durationInMinutes'))
                        if duration:
                            # Convert to seconds if needed
                            if isinstance(duration, str) and ':' in duration:
                                time_parts = duration.split(':')
                                if len(time_parts) == 3:
                                    duration = int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])
                                elif len(time_parts) == 2:
                                    duration = int(time_parts[0]) * 60 + int(time_parts[1])
                            elif isinstance(duration, (int, float)):
                                # Check if this is from durationInMinutes field
                                if 'durationInMinutes' in y['metadata'] and y['metadata']['durationInMinutes'] == duration:
                                    duration = int(duration * 60)  # Convert minutes to seconds
                                elif duration < 1000:  # Likely in minutes
                                    duration = int(duration * 60)
                                else:
                                    duration = int(duration)
                            else:
                                duration = None
                        
                        info_dict = {
                            'id': y['metadata']['contentId'],
                            'name': res['metadata']["title"],
                            #'year': y['metadata']['year'],
                            'season': x['metadata']['season'],
                            'episode': y['metadata']['episodeNumber'],
                            'episodename': y['metadata']['episodeTitle'],
                            'originallang': y['metadata']['language'],
                            'servicedata': y,
                            'duration': duration,
                        }
                        season_data.append(info_dict)

            return [Title(
                id_=x["id"],
                type_=Title.Types.TV,
                name=re.sub(r'\([^)]*\)', '', x["name"]),
                #year=x["year"],
                season=x["season"],
                episode=x["episode"],
                episode_name=re.sub(r'\([^)]*\)', '', x["episodename"]),
                original_lang=x["originallang"],
                source=self.ALIASES[0],
                service_data=x['servicedata'],
                duration=x.get('duration'),
            ) for x in season_data]
    
    #
    def get_lisence(self, id):
        res = self.session.post(
            url=f'https://apiv2.sonyliv.com/AGL/2.4/SR/ENG/FIRE_TV/IN/{self.state_code}/CONTENT/GETLAURL',
            headers={
                "Accept-Encoding": "gzip",
                "Content-Type": "application/json",
                "x-via-device": "true",
                "Host": "apiv2.sonyliv.com",
                "Authorization": self.token,
                "session-id": self.session_id,
                "user-agent": self.config["device"]["user_agent"],
                "build_number": self.config["device"]["build_number"],
                "app_version": self.config["device"]["app_version"],
                "device_id": self.device_id,
            },
            json={
                "actionType": "play",
                "assetId": id,
                "browser": "chrome",
                "deviceId": self.device_id,
                "os": "android",
                "platform": "web"
            }
        ).json()
        return res['resultObj']['laURL']


    def get_manifest(self, id, client):
        r = self.session.post(
            url=self.config["endpoints"]["mpd_url"].format(state_code=self.state_code, media_id=id, contact_id=self.contact_id),
            headers = {
                "Accept-Encoding": "gzip",
                "app_version": self.config["device"]["app_version"],
                "Authorization": self.token,  # Paste your full JWT here
                "build_number": self.config["device"]["build_number"],
                "Content-Type": "application/json",
                "device_id": self.device_id,
                "session_id": self.session_id,
                "td_client_hints": (
                    '{"os_name":"Android",'
                    '"os_version":"12",'
                    '"device_make":"unknown",'
                    '"device_model":"AOSP TV on x86",'
                    '"display_res":"4080",'
                    '"viewport_res":"4080",'
                    '"supp_codec":"AAC,H264,HEVC",'
                    '"audio_decoder":"AAC",'
                    '"hdr_decoder":"NONE",'
                    '"app_version":"6.23.5",'
                    '"td_user_useragent":"com.sonyliv\\/6.23.5 (Android 12; en_XC; AOSP TV on x86; Build\\/STT9.221129.002 )",'
                    '"ram":2,'
                    '"device_type":"tv",'
                    '"is_low_end_device":false,'
                    '"conn_type":"WIFI",'
                    '"client_throughput":1611}'
                ),
                "User-Agent": self.config["device"]["user_agent"],
                "x-via-device": "true",
            }
            headers = {
                "Accept-Encoding": "gzip",
                "app_version": "6.19.4",
                "Authorization": self.token,
                "build_number": "10886",
                "Connection": "Keep-Alive",
                "Content-Type": "application/json",
                "device_id": self.device_id,
                "session_id": self.session_id,
                "User-Agent": "com.sonyliv/6.19.4 (Android 12; en_US; AOSP TV on x86; Build/STT9.221129.002 )",
                "x-via-device": "true",
                "td_client_hints": json.dumps(client, separators=(",", ":"))
            },
            json = {
                "actionType": "play",
                "adsParams": {
                    "app_ver": "6.19.4",
                    "gId": hashlib.md5(uuid.uuid4().bytes).hexdigest(),
                    "Idtype": "adid",
                    "Is_lat": "0",
                    "spty": "subscribed",
                    "vid": str(id)
                },
                "browser": "chrome",
                "deeplinkLang": "",
                "deviceId": self.device_id,
                "hasLAURLEnabled": True,
                "os": "android",
                "platform": "web"
            }
        )
        print(id)
        try:
            return r.json()
        except json.JSONDecodeError:
            raise ValueError(f"Failed to load tracks manifest: {r.text}")

    def get_tracks(self, title):
        # Log duration information if available with enhanced formatting and colors
        if title.duration:
            duration_str = title.get_duration_str()
            content_type = "Movie" if title.type == Title.Types.MOVIE else f"Episode S{title.season:02d}E{title.episode:02d}"
            # Add colors: cyan for emoji, yellow for content type, green for duration
            self.log.info(f"{Colors.CYAN}🎥{Colors.RESET} {Colors.YELLOW}{content_type} Duration:{Colors.RESET} {Colors.GREEN}{duration_str}{Colors.RESET}")

        if self.vcodec == 'H265':
            if self.hdrdv:
                tracks = Tracks()
                client = {
                    '{"os_name":"Android",'
                    f'"os_version":{self.config["device"]["os_version"]},'
                    f'"device_make":{self.config["device"]["brand"]},'
                    f'"device_model":{self.config["device"]["device_model"]},'
                    f'"display_res":{self.config["device"]["device_res"]},'
                    f'"viewport_res":{self.config["device"]["device_res"]},'
                    f'"supp_codec":"HEVC,AAC,EAC3,AC3,ATMOS",'
                    f'"audio_decoder":{self.config["device"]["audio_decoder"]},'
                    f'"hdr_decoder":"DOLBY_VISION",'
                    f'"app_version":{self.config["device"]["app_version"]},'
                    f'"td_user_useragent":{self.config["device"]["td_user_agent"]},'
                    f'"ram":{self.config["device"]["ram"]},'
                    f'"device_type":{self.config["device"]["device_type"]},'
                    f'"is_low_end_device":false,'
                    f'"conn_type":{self.config["device"]["conn_type"]},'
                    f'"client_throughput":{self.config["device"]["client_throughput"]}}'
                }
                res = self.get_manifest(title.id, client)
                dv_url = res["resultObj"]["videoURL"]
                if res.get("isEncrypted") and (
                    "LA_Details" not in res or
                    "laURL" not in res.get("LA_Details", {})
                ):
                    self.license_api = self.get_lisence(title.id)
                else:
                    self.license_api = res["resultObj"]["LA_Details"]["laURL"]
                old_headers = self.session.headers
                
                host_match = re.search(r'https://([^/]+)', dv_url)
                host = host_match.group(1) if host_match else None

                self.session.headers = {
                    #'Host': host,
                    "User-Agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                    "x-playback-session-id": self.device_id,
                }
                self.log.info(dv_url)
                if ".m3u8" in dv_url:
                    dv_tracks = Tracks.from_m3u8(
                        master=m3u8.load(dv_url),
                        source=self.ALIASES[0]
                    )
                    for track in tracks:
                        track.extra = mpd_url
                else:
                    dv_tracks = Tracks.from_mpd(
                        url=dv_url,
                        session=self.session,
                        source=self.ALIASES[0]
                    )
                if self.license_api:
                    for track in dv_tracks:
                        track.license_url = self.license_api
                if res["resultObj"]["subtitle"]:
                    #self.log.info(res["resultObj"]["subtitle"])
                    for x in res["resultObj"]["subtitle"]:
                        sub_url = x["subtitleUrl"]
                        response = requests.get(sub_url)
                        if response.status_code == 200:
                            tracks.add(
                                TextTrack(
                                    id_=md5(sub_url.encode()).hexdigest()[0:6],
                                    source=self.ALIASES[0],
                                    url=sub_url,
                                    codec='vtt',
                                    language=x["subtitleLanguageName"],
                                    forced=False,
                                    sdh=False
                                )
                            )
                
                client = {
                    '{"os_name":"Android",'
                    f'"os_version":{self.config["device"]["os_version"]},'
                    f'"device_make":{self.config["device"]["brand"]},'
                    f'"device_model":{self.config["device"]["device_model"]},'
                    f'"display_res":{self.config["device"]["device_res"]},'
                    f'"viewport_res":{self.config["device"]["device_res"]},'
                    f'"supp_codec":"HEVC,AAC,EAC3,AC3,ATMOS",'
                    f'"audio_decoder":{self.config["device"]["audio_decoder"]},'
                    f'"hdr_decoder":"HDR10",'
                    f'"app_version":{self.config["device"]["app_version"]},'
                    f'"td_user_useragent":{self.config["device"]["td_user_agent"]},'
                    f'"ram":{self.config["device"]["ram"]},'
                    f'"device_type":{self.config["device"]["device_type"]},'
                    f'"is_low_end_device":false,'
                    f'"conn_type":{self.config["device"]["conn_type"]},'
                    f'"client_throughput":{self.config["device"]["client_throughput"]}}'
                }
                self.session.headers = old_headers
                res = self.get_manifest(title.id, client)
                hdr_url = res["resultObj"]["videoURL"]
                if res.get("isEncrypted") and (
                    "LA_Details" not in res or
                    "laURL" not in res.get("LA_Details", {})
                ):
                    self.license_api = self.get_lisence(title.id)
                else:
                    self.license_api = res["resultObj"]["LA_Details"]["laURL"]

                host_match = re.search(r'https://([^/]+)', hdr_url)
                host = host_match.group(1) if host_match else None
                self.session.headers = {
                    #'Host': host,
                    "User-Agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
                    "x-playback-session-id": self.device_id,
                }
                self.log.info(hdr_url)
                if ".m3u8" in hdr_url:
                    hdr_tracks = Tracks.from_m3u8(
                        master=m3u8.load(hdr_url),
                        source=self.ALIASES[0]
                    )
                    for track in tracks:
                        track.extra = mpd_url
                else:
                    hdr_tracks = Tracks.from_mpd(
                        url=hdr_url,
                        session=self.session,
                        source=self.ALIASES[0]
                    )
                if self.license_api:
                    for track in hdr_tracks:
                        track.license_url = self.license_api
                if res["resultObj"]["subtitle"]:
                    #self.log.info(res["resultObj"]["subtitle"])
                    for x in res["resultObj"]["subtitle"]:
                        sub_url = x["subtitleUrl"]
                        response = requests.get(sub_url)
                        if response.status_code == 200:
                            tracks.add(
                                TextTrack(
                                    id_=md5(sub_url.encode()).hexdigest()[0:6],
                                    source=self.ALIASES[0],
                                    url=sub_url,
                                    codec='vtt',
                                    language=x["subtitleLanguageName"],
                                    forced=False,
                                    sdh=False
                                )
                            )

                if self.range == 'HDR10' and "hdr" in hdr_url:
                    for track in hdr_tracks.videos:
                        if not track.hdr10:
                            track.hdr10 = True

                    tracks.add(dv_tracks, warn_only=True)
                    tracks.add(hdr_tracks, warn_only=True)

                for track in tracks:
                    track.needs_proxy = True
                hdr_tracks = [v for v in tracks.videos if getattr(v, "hdr10", False)]
                dv_tracks  = [v for v in tracks.videos if getattr(v, "dv", False)]

                if hdr_tracks:
                    best_hdr = sorted(hdr_tracks, key=lambda x: (x.height, x.bitrate), reverse=True)[0]
                    best_hdr.selected = True
                    self.log.info(f"Selected HDR base: {best_hdr}")

                if dv_tracks:
                    best_dv = sorted(dv_tracks, key=lambda x: (x.height, x.bitrate), reverse=True)[0]
                    best_dv.selected = True
                    self.log.info(f"Selected DV track: {best_dv}")
                tracks.range = "HDRDV"
                return tracks

            else:
                if self.range == 'DV':
                    client = {
                        '{"os_name":"Android",'
                        f'"os_version":{self.config["device"]["os_version"]},'
                        f'"device_make":{self.config["device"]["brand"]},'
                        f'"device_model":{self.config["device"]["device_model"]},'
                        f'"display_res":{self.config["device"]["device_res"]},'
                        f'"viewport_res":{self.config["device"]["device_res"]},'
                        f'"supp_codec":"HEVC,AAC,EAC3,AC3,ATMOS",'
                        f'"audio_decoder":{self.config["device"]["audio_decoder"]},'
                        f'"hdr_decoder":"DOLBY_VISION",'
                        f'"app_version":{self.config["device"]["app_version"]},'
                        f'"td_user_useragent":{self.config["device"]["td_user_agent"]},'
                        f'"ram":{self.config["device"]["ram"]},'
                        f'"device_type":{self.config["device"]["device_type"]},'
                        f'"is_low_end_device":false,'
                        f'"conn_type":{self.config["device"]["conn_type"]},'
                        f'"client_throughput":{self.config["device"]["client_throughput"]}}'
                    }
                elif self.range == 'HDR10':
                    client = {
                        '{"os_name":"Android",'
                        f'"os_version":{self.config["device"]["os_version"]},'
                        f'"device_make":{self.config["device"]["brand"]},'
                        f'"device_model":{self.config["device"]["device_model"]},'
                        f'"display_res":{self.config["device"]["device_res"]},'
                        f'"viewport_res":{self.config["device"]["device_res"]},'
                        f'"supp_codec":"HEVC,AAC,EAC3,AC3,ATMOS",'
                        f'"audio_decoder":{self.config["device"]["audio_decoder"]},'
                        f'"hdr_decoder":"HDR10",'
                        f'"app_version":{self.config["device"]["app_version"]},'
                        f'"td_user_useragent":{self.config["device"]["td_user_agent"]},'
                        f'"ram":{self.config["device"]["ram"]},'
                        f'"device_type":{self.config["device"]["device_type"]},'
                        f'"is_low_end_device":false,'
                        f'"conn_type":{self.config["device"]["conn_type"]},'
                        f'"client_throughput":{self.config["device"]["client_throughput"]}}'
                    }
                elif self.range == 'SDR':
                    client = {
                        '{"os_name":"Android",'
                        f'"os_version":{self.config["device"]["os_version"]},'
                        f'"device_make":{self.config["device"]["brand"]},'
                        f'"device_model":{self.config["device"]["device_model"]},'
                        f'"display_res":{self.config["device"]["device_res"]},'
                        f'"viewport_res":{self.config["device"]["device_res"]},'
                        f'"supp_codec":"HEVC,AAC,EAC3,AC3,ATMOS",'
                        f'"audio_decoder":{self.config["device"]["audio_decoder"]},'
                        f'"hdr_decoder":"HLG",'
                        f'"app_version":{self.config["device"]["app_version"]},'
                        f'"td_user_useragent":{self.config["device"]["td_user_agent"]},'
                        f'"ram":{self.config["device"]["ram"]},'
                        f'"device_type":{self.config["device"]["device_type"]},'
                        f'"is_low_end_device":false,'
                        f'"conn_type":{self.config["device"]["conn_type"]},'
                        f'"client_throughput":{self.config["device"]["client_throughput"]}}'
                    }
        else:
            client = {
                '{"os_name":"Android",'
                f'"os_version":{self.config["device"]["os_version"]},'
                f'"device_make":{self.config["device"]["brand"]},'
                f'"device_model":{self.config["device"]["device_model"]},'
                f'"display_res":{self.config["device"]["device_res"]},'
                f'"viewport_res":{self.config["device"]["device_res"]},'
                f'"supp_codec":"H264,AV1,AAC,AC3,EAC3",'
                f'"audio_decoder":{self.config["device"]["audio_decoder"]},'
                f'"hdr_decoder":"UNKNOWN",'
                f'"app_version":{self.config["device"]["app_version"]},'
                f'"td_user_useragent":{self.config["device"]["td_user_agent"]},'
                f'"ram":{self.config["device"]["ram"]},'
                f'"device_type":{self.config["device"]["device_type"]},'
                f'"is_low_end_device":false,'
                f'"conn_type":{self.config["device"]["conn_type"]},'
                f'"client_throughput":{self.config["device"]["client_throughput"]}}'
            }
        res = self.get_manifest(title.id, client)

        mpd_url = res["resultObj"]["videoURL"]
        self.log.info(mpd_url)
        
        if res.get("isEncrypted") and (
            "LA_Details" not in res or
            "laURL" not in res.get("LA_Details", {})
        ):
            self.license_api = self.get_lisence(title.id)
        else:
            self.license_api = res["resultObj"]["LA_Details"]["laURL"]

        host_match = re.search(r'https://([^/]+)', mpd_url)
        host = host_match.group(1) if host_match else None

        self.session.headers = {
            #'Host': host,
            "User-Agent": "com.onemainstream.sonyliv.android/8.95 (Android 7.1.2; en_IN; AFTMM; Build/NS6281 )",
            "x-playback-session-id": self.device_id,
        }

        if ".m3u8" in mpd_url:
            tracks = Tracks.from_m3u8(
                master=m3u8.load(mpd_url),
                source=self.ALIASES[0]
            )
            for track in tracks:
                track.extra = mpd_url
        else:
            tracks = Tracks.from_mpd(
                url=mpd_url,
                session=self.session,
                source=self.ALIASES[0]
            )

        if self.vcodec == 'H264' and res["resultObj"]["Maximum_Resolution"] == "UHD":
            self.log.info(" + Checking for UHD...")
            client = {
                '{"os_name":"Android",'
                f'"os_version":{self.config["device"]["os_version"]},'
                f'"device_make":{self.config["device"]["brand"]},'
                f'"device_model":{self.config["device"]["device_model"]},'
                f'"display_res":{self.config["device"]["device_res"]},'
                f'"viewport_res":{self.config["device"]["device_res"]},'
                f'"supp_codec":"H265,H264,AAC,EAC3,AC3,ATMOS",'
                f'"audio_decoder":{self.config["device"]["audio_decoder"]},'
                f'"hdr_decoder":"HLG",'
                f'"app_version":{self.config["device"]["app_version"]},'
                f'"td_user_useragent":{self.config["device"]["td_user_agent"]},'
                f'"ram":{self.config["device"]["ram"]},'
                f'"device_type":{self.config["device"]["device_type"]},'
                f'"is_low_end_device":false,'
                f'"conn_type":{self.config["device"]["conn_type"]},'
                f'"client_throughput":{self.config["device"]["client_throughput"]}}'
            }
            res = self.get_manifest(title.id, client)

            audio_mpd_url = res["resultObj"]["videoURL"]
            self.log.info(audio_mpd_url)

            audio_tracks = Tracks.from_mpd(
                url=audio_mpd_url,
                session=self.session,
                source=self.ALIASES[0]
            )
            tracks.audios = audio_tracks.audios

        #"""
        if res["resultObj"]["subtitle"]:
            #self.log.info(res["resultObj"]["subtitle"])
            for x in res["resultObj"]["subtitle"]:
                sub_url = x["subtitleUrl"]
                response = requests.get(sub_url)
                if response.status_code == 200:
                    tracks.add(
                        TextTrack(
                            id_=md5(sub_url.encode()).hexdigest()[0:6],
                            source=self.ALIASES[0],
                            url=sub_url,
                            codec='vtt',
                            language=x["subtitleLanguageName"],
                            forced=False,
                            sdh=False
                        )
                    )
        #"""

        if self.license_api:
            for track in tracks:
                track.license_url = self.license_api

        if self.range == 'HDR10' and "hdr" in mpd_url:
            for track in tracks.videos:
                if not track.hdr10:
                    track.hdr10 = True

        # Store manifest URL for N_m3u8DL-RE
        from vinetrimmer.utils.n_m3u8dl_config import store_manifest_url
        store_manifest_url(tracks, mpd_url, self.ALIASES[0])

        for track in tracks:
            track.needs_proxy = True

        return tracks


    def get_chapters(self, title):
        return []


    def certificate(self, **_):
        return None  # will use common privacy cert


    def license(self, challenge, track, **_):
        return requests.post(
            url=track.license_url,
            data=challenge,  # expects bytes
            headers={
                "Accept-Encoding": "gzip",
                "Content-Type": "application/octet-stream",
                "Host": "wv-sony.service.expressplay.com",
                "User-Agent": self.config["device"]["user_agent"],
                'x-playback-session-id': self.device_id,
            }
        ).content

    # Service specific functions

    def configure(self):
        self.log.info("Logging into SonyLiv")
        self.device_id = self.get_device_id()
        self.log.info(f" + Using Device ID: {self.device_id}")
        self.session_id = f'{str(uuid.uuid4().hex)}'
        self.state_code, self.city, self.channelpartnerid = self.get_ULD()
        self.token = self.get_token()
        self.contact_id = self.get_contact_id()

    #
    def get_ULD(self):
        headers = {
            "Accept-Encoding": "gzip",
            "app_version": self.config["device"]["app_version"],
            "build_number": self.config["device"]["build_number"],
            "Connection": "Keep-Alive",
            "Content-Type": "application/json",
            "device_id": self.device_id,
            "session_id": self.session_id,
            "user-agent": self.config["device"]["user_agent"],
            "x-via-device": "true"
        }
        # Step 1: Make request
        res = self.session.get(self.config["endpoints"]["uld_url"], headers=headers)
        data = res.json()
        # Step 2: Extract values safely
        result = data.get("resultObj", {})
        state_code = res.headers.get("state_code")
        city = res.headers.get("city")
        channel_partner_id = result.get("channelPartnerID")
        return state_code, city, channel_partner_id


    def get_contact_id(self):
        contact_id_url = self.config["endpoints"]["get_contact_id"].format(
            state_code=self.state_code,
            channelpartnerid=self.channelpartnerid
        )
        data = self.session.get(self.config["endpoints"]["get_contact_id"]
            url=contact_id_url,
            headers={
                "Accept-Encoding": "gzip",
                "Content-Type": "application/json",
                "Connection": "Keep-Alive",
                "x-via-device": "true",
                "Host": "apiv2.sonyliv.com",
                "session-id": self.session_id,
                "user-agent": self.config["device"]["user_agent"],
                "build_number": self.config["device"]["build_number"],
                "app_version": self.config["device"]["app_version"],
                "Authorization": self.token,
                "device_id": self.device_id,
            }
        ).json()
        return data['resultObj']['contactMessage'][0].get('contactID')


    def get_device_id(self):
        to = self.get_cache("deviceid_{profile}.json".format(profile=self.profile))
        if os.path.isfile(to):
            with open(to, encoding="utf-8") as fd:
                device_id = json.load(fd)
                return device_id['deviceid']
        unique_id = uuid.uuid4().hex[:16]
        data = {"deviceid": unique_id}
        os.makedirs(os.path.dirname(to), exist_ok=True)
        with open(to, "w", encoding="utf-8") as fd:
            json.dump(data, fd)
        return unique_id


    def get_token(self):
        token_cache_path = self.get_cache("token_{profile}.json".format(profile=self.profile))
        if os.path.isfile(token_cache_path):
            with open(token_cache_path, encoding="utf-8") as fd:
                token = json.load(fd)
            if True:
                # not expired, lets use
                self.log.info(" + Using cached auth tokens...")
                return token["access_token"]
        # get new token
        token = self.login()
        return self.save_token(token, token_cache_path)


    @staticmethod
    def save_token(token, to):
        os.makedirs(os.path.dirname(to), exist_ok=True)
        with open(to, "w", encoding="utf-8") as fd:
            json.dump({'access_token': token}, fd)
        return token


    def login(self):
        headers = {
            "Accept-Encoding": "gzip",
            "app_version": self.config["device"]["app_version"],
            "build_number": self.config["device"]["build_number"],
            "Connection": "Keep-Alive",
            "Content-Type": "application/json",
            "device_id": self.device_id,
            "session_id": self.session_id,
            "user-agent": self.config["device"]["user_agent"],
            "x-via-device": "true",
        }
        payload = {
            "channelPartnerID": self.channelpartnerid,
            "deviceBrand": self.config["device"]["brand"],
            "deviceModelNumber": self.config["device"]["model_number"],
            "deviceName": self.config["device"]["device_name"],
            "deviceType": self.config["device"]["device_type"],
            "location": self.city,
            "reset": True,
            "serialNo": self.device_id
        }
        res = self.session.post(url=self.config["endpoints"]["generate_device_code"].format(state_code=self.state_code), headers=headers, json=payload).json()
        code = res['resultObj']['activationCode']
        total_attempts = 300
        interval = 1  # seconds

        self.log.info(
            f"LOGIN_REQUIRED|{self.config['endpoints']['activate_url']}|{code}|{total_attempts * interval}"
        )

        # ✅ polling loop (USE CORRECT API HERE)
        for i in range(total_attempts):
            try:
                r = self.session.post(
                    url=self.config["endpoints"]["verify_code"].format(state_code=self.state_code),
                    headers = {
                        "Accept-Encoding": "gzip",
                        "app_version": self.config["device"]["app_version"],
                        "build_number": self.config["device"]["build_number"],
                        "Connection": "Keep-Alive",
                        "Content-Type": "application/json",
                        "device_id": self.device_id,
                        "session_id": self.session_id,
                        "user-agent": self.config["device"]["user_agent"],
                        "x-via-device": "true",
                    },
                    json={
                        "channelPartnerID": self.channelpartnerid,
                        "deviceBrand": self.config["device"]["brand"],
                        "deviceModelNumber": self.config["device"]["model_number"],
                        "deviceName": self.config["device"]["device_name"],
                        "deviceType": self.config["device"]["device_type"],
                        "location": self.city,
                        "reset": False,
                        "serialNo": self.device_id
                    }
                ).json()

                result = r.get("resultObj", {})

                # ✅ SUCCESS
                if "accessToken" in result:
                    self.log.info("LOGIN_SUCCESS")
                    return result["accessToken"]

            except Exception as e:
                self.log.info(f"LOGIN_TIMEOUT|{str(e)}")

            time.sleep(interval)

        # ⛔ TIMEOUT
        self.log.info("LOGIN_TIMEOUT")
        return None
