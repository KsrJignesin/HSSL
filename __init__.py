import hashlib
import json
import re
from collections.abc import Generator
from datetime import datetime
from http.cookiejar import CookieJar
from typing import Optional, Union

import click
from langcodes import Language

from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.manifests import DASH
from unshackle.core.search_result import SearchResult
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from unshackle.core.tracks import Chapter, Subtitle, Tracks, Video


class Tabii(Service):
    """
    Service code for Tabii (tabii.com)
    Version: 1.1.0
    Author: Riyadh
    
    \b
    Authorization: Credentials
    Security: FHD@L3
    
    \b
    Supported URL formats:
    - Direct title ID: 527983
    - Watch URL: https://www.tabii.com/watch/527983
    - Browse URL: https://www.tabii.com/browse/149106_149112?dt=527983
    - Episode URL: https://www.tabii.com/watch/527983?trackId=545891
    - Movie URL: https://www.tabii.com/browse/149265_149266?dt=544916
    
    \b
    Features:
    - Automatic token caching and refresh
    - Support for both movies and series
    - Multi-language audio and subtitles
    - Widevine L3 DRM decryption
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?tabii\.com/(?:watch|browse)/(?:[^?]+\?dt=)?)?(?P<title_id>[^/?&]+)(?:\?trackId=(?P<track_id>[^&]+))?"
    GEOFENCE = ("TR",)

    @staticmethod
    @click.command(name="Tabii", short_help="https://tabii.com")
    @click.argument("title", type=str)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Specify if it's a movie")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Tabii(ctx, **kwargs)

    def __init__(self, ctx, title, movie):
        super().__init__(ctx)

        self.title = title
        self.movie = movie
        self.cdm = ctx.obj.cdm

        if self.config is None:
            raise Exception("Tabii service configuration is missing! Please add config.yaml to the service directory.")

        self.access_token = None
        self.refresh_token = None
        self.device = "android"
        self.license_url = None
        
        # Validate required config sections
        required_sections = ["endpoints", "client"]
        for section in required_sections:
            if section not in self.config:
                raise Exception(f"Missing required config section: {section}")

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        """Authenticate with Tabii service using email and password"""
        super().authenticate(cookies, credential)
        
        if not credential:
            raise EnvironmentError("Tabii requires email and password credentials for authentication.")

        # Setup basic session headers
        self.log.debug("Setting up session headers")
        self.session.headers.update({
            "User-Agent": self.config["client"][self.device]["user_agent"],
            "Accept-Encoding": "gzip",
            "Accept-Language": "en",
            "Connection": "Keep-Alive"
        })

        # Try cached tokens first
        cache = self.cache.get(f"tabii_tokens")

        if cache and cache.data:
            expires_at = cache.data.get("expires_at", 0)
            current_time = int(datetime.now().timestamp())
            
            if expires_at > current_time:
                self.log.info("Using cached authentication tokens")
                self.log.debug(f"Token valid for {(expires_at - current_time) // 60} more minutes")
                self.access_token = cache.data["access_token"]
                self.refresh_token = cache.data["refresh_token"]
                self._set_full_headers()
            else:
                self.log.info("Cached tokens expired, attempting refresh")
                if not self._refresh_token(cache.data.get("refresh_token")):
                    self.log.warning("Token refresh failed, performing new login")
                    self._perform_login(credential.username, credential.password)
                else:
                    self.log.info("Successfully refreshed authentication tokens")
        else:
            self.log.info("No cached tokens found, performing new login")
            self._perform_login(credential.username, credential.password)

        # Set authorization header with final token
        self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})
        self.log.debug("Authentication completed successfully")

    def _set_full_headers(self) -> None:
        """Set all required headers after authentication"""
        self.log.debug("Configuring full session headers")
        self.session.headers.update(self.config["client"][self.device]["headers"])
        self.session.headers.update({"User-Agent": self.config["client"][self.device]["user_agent"]})

    def _perform_login(self, email: str, password: str) -> None:
        """Perform complete login flow: initial auth -> profile selection -> profile token"""
        
        self.log.info("Starting Tabii authentication flow")
        
        # Step 1: Initial login
        self.log.debug("Step 1/3: Requesting initial access token")
        login_headers = {
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
            "Connection": "Keep-Alive",
            "Trtdigitalprod": self.config["client"][self.device]["headers"]["Trtdigitalprod"],
            "User-Agent": self.config["client"][self.device]["user_agent"],
            "x-client-useragent": self.config["client"][self.device]["headers"]["x-client-useragent"],
            "x-clientid": self.config["client"][self.device]["headers"]["x-clientid"],
            "x-clientpass": self.config["client"][self.device]["headers"]["x-clientpass"],
            "x-uid": self.config["client"][self.device]["headers"]["x-uid"]
        }
        
        try:
            login_req = self.session.post(
                url=self.config["endpoints"]["login"],
                json={"email": email, "password": password},
                headers=login_headers,
                timeout=30
            )
            
            if login_req.status_code != 200:
                error_msg = f"Login failed with HTTP {login_req.status_code}"
                try:
                    error_data = login_req.json()
                    if "message" in error_data:
                        error_msg += f": {error_data['message']}"
                except:
                    error_msg += f": {login_req.text[:200]}"
                
                self.log.error(error_msg)
                raise Exception(error_msg)
            
            login_response = login_req.json()
            self.log.debug("Initial authentication successful")
            
        except Exception as e:
            self.log.error(f"Login request failed: {str(e)}")
            raise Exception(f"Failed to authenticate with Tabii: {str(e)}")
        
        profile_access_token = login_response.get("accessToken")
        profile_refresh_token = login_response.get("refreshToken")

        if not profile_access_token or not profile_refresh_token:
            error_msg = login_response.get("message", "Unknown error - missing tokens in response")
            self.log.error(f"Failed to obtain access tokens: {error_msg}")
            raise Exception(f"Authentication failed: {error_msg}")

        # Step 2: Get profiles
        self.log.debug("Step 2/3: Fetching available profiles")
        self.session.headers.update({"Authorization": f"Bearer {profile_access_token}"})
        
        try:
            profiles_response = self.session.get(
                url=self.config["endpoints"]["profiles"],
                timeout=30
            ).json()
        except Exception as e:
            self.log.error(f"Failed to fetch profiles: {str(e)}")
            raise Exception(f"Could not retrieve profile list: {str(e)}")

        # Find non-kids profile
        profile_sk = None
        profile_name = None
        profiles_data = profiles_response.get("data", [])
        
        self.log.debug(f"Found {len(profiles_data)} profile(s)")
        
        for profile in profiles_data:
            kids_value = profile.get("kids", False)
            if isinstance(kids_value, str):
                kids_value = kids_value.lower() == "true"
            
            if not kids_value:
                profile_sk = profile.get("SK")
                profile_name = profile.get("name", "Unknown")
                self.log.debug(f"Selected profile: {profile_name}")
                break

        if not profile_sk:
            self.log.error("No suitable adult profile found in account")
            raise Exception("No adult profile available. Please create a non-kids profile in your Tabii account.")

        # Step 3: Get profile token
        self.log.debug(f"Step 3/3: Requesting token for profile '{profile_name}'")
        
        try:
            profile_token_response = self.session.post(
                url=self.config["endpoints"]["profile_token"].format(profile_sk=profile_sk),
                json={"refreshToken": profile_refresh_token},
                headers={"Content-Type": "application/json"},
                timeout=30
            ).json()
        except Exception as e:
            self.log.error(f"Failed to get profile token: {str(e)}")
            raise Exception(f"Could not obtain profile-specific token: {str(e)}")

        self.access_token = profile_token_response.get("accessToken")
        self.refresh_token = profile_token_response.get("refreshToken")

        if not self.access_token or not self.refresh_token:
            self.log.error("Profile token response missing required tokens")
            raise Exception("Failed to obtain profile tokens")

        self._set_full_headers()

        # Cache tokens with 24 hour expiry
        cache_data = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": int(datetime.now().timestamp()) + 86400,
            "profile_name": profile_name
        }
        self.cache.get(f"tabii_tokens").set(data=cache_data)
        
        self.log.info(f"Successfully authenticated as profile '{profile_name}'")
        self.log.debug("Authentication tokens cached for 24 hours")

    def _refresh_token(self, refresh_token: str) -> bool:
        """Attempt to refresh access token using refresh token"""
        
        if not refresh_token:
            self.log.debug("No refresh token available")
            return False
            
        self.log.debug("Attempting to refresh access token")
        
        try:
            refresh_headers = {
                "Content-Type": "application/json; charset=UTF-8",
                "Trtdigitalprod": self.config["client"][self.device]["headers"]["Trtdigitalprod"],
                "x-clientid": "ANDROID",
                "x-clientpass": "000"
            }
            
            response = self.session.post(
                url=self.config["endpoints"]["token_refresh"],
                json={"refreshToken": refresh_token},
                headers=refresh_headers,
                timeout=30
            )
            
            if response.status_code != 200:
                self.log.warning(f"Token refresh failed")
                return False
                
            response_data = response.json()
            self.access_token = response_data.get("accessToken")
            new_refresh_token = response_data.get("refreshToken")

            if not self.access_token:
                self.log.warning("Token refresh response missing access token")
                return False

            self.refresh_token = new_refresh_token or refresh_token
            self._set_full_headers()

            # Update cache
            cache_data = {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": int(datetime.now().timestamp()) + 86400
            }
            self.cache.get(f"tabii_tokens").set(data=cache_data)

            self.log.debug("Access token refreshed successfully")
            return True
            
        except Exception as e:
            self.log.warning(f"Token refresh failed: {str(e)}")
            return False

    def search(self) -> Generator[SearchResult, None, None]:
        """Search functionality - not implemented for Tabii"""
        self.log.debug("Search called - Tabii does not support search, returning placeholder")
        yield SearchResult(
            id_=self.title,
            title="Direct URL or ID required",
            label="N/A",
            url=f"https://www.tabii.com/watch/{self.title}"
        )

    def get_titles(self) -> Titles_T:
        """Fetch title metadata from Tabii API"""
        
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            self.log.error(f"Invalid title format: {self.title}")
            raise Exception(f"Invalid title URL or ID format. Expected format: '527983' or 'https://www.tabii.com/watch/527983'")

        title_id = match.group("title_id")
        track_id = match.group("track_id")
        
        self.log.info(f"Fetching metadata for title ID: {title_id}")
        if track_id:
            self.log.debug(f"Specific episode requested: {track_id}")

        try:
            metadata = self.session.get(
                url=self.config["endpoints"]["metadata"].format(title_id=title_id),
                timeout=30
            ).json()
        except Exception as e:
            self.log.error(f"Failed to fetch title metadata: {str(e)}")
            raise Exception(f"Could not retrieve title information: {str(e)}")

        if "data" not in metadata:
            self.log.error("API response missing 'data' field")
            raise Exception("Invalid API response format")

        content_type = metadata.get("data", {}).get("contentType", "").lower()
        title_name = metadata["data"].get("title", "Unknown")
        
        self.log.debug(f"Content type: {content_type}, Title: {title_name}")
        
        if content_type == "movie":
            self.movie = True

        if self.movie:
            return self._parse_movie(metadata)
        else:
            return self._parse_series(metadata, track_id)

    def _parse_movie(self, metadata: dict) -> Movies:
        """Parse movie metadata"""
        
        current_content = metadata["data"].get("currentContent", {})
        current_content_id = current_content.get("id")
        
        if not current_content_id:
            self.log.error("Movie content ID not found in metadata")
            raise Exception("Could not find movie content ID")
        
        title = metadata["data"]["title"]
        year = int(metadata["data"].get("madeYear", 0)) if metadata["data"].get("madeYear") else None
        
        self.log.info(f"Parsed movie: {title} ({year})")
        self.log.debug(f"Content ID: {current_content_id}")
        
        return Movies([
            Movie(
                id_=current_content_id,
                service=self.__class__,
                name=title,
                description=metadata["data"].get("description", ""),
                year=year,
                language=Language.get("tr"),
                data=current_content
            )
        ])

    def _parse_series(self, metadata: dict, track_id: Optional[str] = None) -> Series:
        """Parse series metadata and episodes"""
        
        episodes = []
        seasons = metadata["data"].get("seasons", [])
        series_title = metadata["data"]["title"]
        series_year = int(metadata["data"].get("madeYear", 0)) if metadata["data"].get("madeYear") else None
        
        self.log.info(f"Parsing series: {series_title} ({series_year})")
        self.log.debug(f"Found {len(seasons)} season(s)")
        
        for season in seasons:
            season_number = season.get("seasonNumber", 1)
            season_episodes = season.get("episodes", [])
            
            self.log.debug(f"Season {season_number}: {len(season_episodes)} episode(s)")
            
            for episode in season_episodes:
                episode_id = episode["id"]
                
                # Filter by track_id if specified
                if track_id and episode_id != track_id:
                    continue
                
                episode_number = int(episode.get("episodeNumber", 0))
                episode_title = episode.get("title", "")
                
                # Clean episode title (remove leading numbers like "1. Episode Title")
                if "." in episode_title:
                    parts = episode_title.split(".", 1)
                    if parts[0].strip().isdigit():
                        episode_title = parts[1].strip()
                
                self.log.debug(f"Added S{season_number:02d}E{episode_number:02d}: {episode_title}")
                
                episodes.append(Episode(
                    id_=episode_id,
                    service=self.__class__,
                    title=series_title,
                    season=season_number,
                    number=episode_number,
                    name=episode_title,
                    description=episode.get("description", ""),
                    year=series_year,
                    language=Language.get("tr"),
                    data=episode
                ))
        
        if not episodes:
            if track_id:
                self.log.error(f"Episode with ID {track_id} not found")
                raise Exception(f"Episode ID {track_id} not found in series")
            else:
                self.log.error("No episodes found in series")
                raise Exception("No episodes found in series metadata")
        
        self.log.info(f"Successfully parsed {len(episodes)} episode(s)")
        return Series(episodes)

    def get_tracks(self, title: Title_T) -> Tracks:
        """Get video, audio, and subtitle tracks for a title"""
        
        self.log.info(f"Fetching tracks for: {title.name if hasattr(title, 'name') else title.id}")
        
        # Get media array from title data
        media_array = title.data.get("media", []) if hasattr(title, 'data') else []
        
        # If not found, try fetching from API
        if not media_array:
            self.log.debug("Media array not in title data, fetching from API")
            media_array = self._fetch_media_array(title)

        if not media_array:
            self.log.error("No media information found for title")
            raise Exception("No media streams available for this title")

        # Find best available stream
        manifest_url, resource_id = self._select_media_stream(media_array)
        
        if not manifest_url:
            self.log.error("No valid media stream found")
            raise Exception("No compatible media stream found (Widevine or Clear)")

        # Get DRM ticket if needed
        if resource_id:
            self.log.debug("Content is DRM-protected, requesting entitlement ticket")
            self._request_drm_ticket(title.id, resource_id)
        else:
            self.log.info("Content is DRM-free")
            self.license_url = None

        # Parse DASH manifest
        self.log.debug(f"Parsing manifest: {manifest_url}")
        try:
            tracks = DASH.from_url(url=manifest_url, session=self.session).to_tracks(language=Language.get("tr"))
        except Exception as e:
            self.log.error(f"Failed to parse manifest: {str(e)}")
            raise Exception(f"Could not parse video manifest: {str(e)}")

        # Configure video tracks
        for video in tracks.videos:
            video.range = Video.Range.SDR
            video.closed_captions = False  # Prevent ccextractor calls
        
        self.log.info(f"Found {len(tracks.videos)} video, {len(tracks.audio)} audio, {len(tracks.subtitles)} subtitle track(s)")
        
        return tracks

    def _fetch_media_array(self, title: Title_T) -> list:
        """Fetch media array from API if not in title data"""
        
        show_id = self.title.split('?')[0] if '?' in self.title else self.title
        match = re.match(self.TITLE_RE, show_id)
        if match:
            show_id = match.group("title_id")
        
        try:
            metadata = self.session.get(
                url=self.config["endpoints"]["metadata"].format(title_id=show_id),
                timeout=30
            ).json()
        except Exception as e:
            self.log.error(f"Failed to fetch media metadata: {str(e)}")
            return []
        
        # Search for episode in metadata
        for season in metadata["data"].get("seasons", []):
            for episode in season.get("episodes", []):
                if episode["id"] == title.id:
                    self.log.debug(f"Found media array for episode {title.id}")
                    return episode.get("media", [])
        
        return []

    def _select_media_stream(self, media_array: list) -> tuple[Optional[str], Optional[str]]:
        """Select best available media stream (prefer Widevine over Clear)"""
        
        manifest_url = None
        resource_id = None
        
        # Try Widevine first
        for media in media_array:
            if media.get("drmSchema") == "widevine":
                manifest_url = self.config["endpoints"]["playback_manifest"].replace(
                    "{media_url}", media["url"]
                ) + "?width=-1&height=-1&bandwidth=-1&subtitleType=vtt"
                resource_id = media.get("resourceId")
                self.log.debug(f"Selected Widevine stream, resource ID: {resource_id}")
                break
        
        # Fallback to Clear if no Widevine
        if not manifest_url:
            for media in media_array:
                if media.get("drmSchema") == "clear":
                    manifest_url = self.config["endpoints"]["playback_manifest"].replace(
                        "{media_url}", media["url"]
                    ) + "?width=-1&height=-1&bandwidth=-1&subtitleType=vtt"
                    self.log.info("Selected DRM-free (Clear) stream")
                    break
        
        return manifest_url, resource_id

    def _request_drm_ticket(self, entitlement_id: str, resource_id: str) -> None:
        """Request DRM entitlement ticket"""
        
        try:
            ticket_response = self.session.get(
                url=self.config["endpoints"]["entitlement_ticket"].format(entitlement_id=entitlement_id),
                timeout=30
            ).json()
        except Exception as e:
            self.log.error(f"Failed to get entitlement ticket: {str(e)}")
            raise Exception(f"Could not obtain DRM license ticket: {str(e)}")
        
        ticket = ticket_response.get("ticket")
        if not ticket:
            self.log.error("Entitlement ticket not found in response")
            raise Exception("Failed to obtain DRM entitlement. Check your subscription status.")
        
        self.license_url = f"{self.config['endpoints']['widevine_license']}?ticket={ticket}&resource_id={resource_id}"
        self.log.debug("DRM license URL configured successfully")

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        """Get chapter information - not available for Tabii content"""
        return []

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        """Request Widevine license for DRM-protected content"""
        
        if not self.license_url:
            self.log.error("No license URL available for DRM content")
            raise Exception("License URL not configured (content may be DRM-free)")

        self.log.debug(f"Requesting Widevine license for track: {track.id if hasattr(track, 'id') else 'unknown'}")
        
        try:
            response = self.session.post(
                url=self.license_url,
                data=challenge,
                headers={
                    "Content-Type": "application/octet-stream",
                    "User-Agent": self.config["client"][self.device]["license_user_agent"],
                    "Trtdigitalprod": self.config["client"][self.device]["headers"]["Trtdigitalprod"]
                },
                timeout=30
            )
            
            response.raise_for_status()
            self.log.debug(f"Received license response ({len(response.content)} bytes)")
            return response.content
            
        except Exception as e:
            self.log.error(f"License request failed: {str(e)}")
            raise Exception(f"Failed to obtain Widevine license: {str(e)}")