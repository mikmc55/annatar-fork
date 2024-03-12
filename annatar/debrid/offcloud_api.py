import asyncio
from typing import Any, AsyncGenerator

import aiohttp
import structlog
from pydantic import TypeAdapter

from annatar.debrid import magnet
from annatar.debrid.debrid_service import DebridService
from annatar.debrid.models import StreamLink
from annatar.debrid.offcloud_models import (
    AddMagnetResponse,
    CacheResponse,
    CloudHistoryItem,
    CloudStatusResponse,
    TorrentInfo,
)
from annatar.human import is_video
from annatar.torrent import TorrentMeta

log = structlog.get_logger(__name__)


class OffCloud(DebridService):
    BASE_URL = "https://offcloud.com/api"

    async def make_request(
        self,
        method: str,
        url: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        params = params or {}
        params["key"] = self.api_key
        url = self.BASE_URL + url

        async with aiohttp.ClientSession() as session, session.request(
            method,
            url,
            data=data,
            params=params,
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def add_magent_link(self, magnet_link: str) -> AddMagnetResponse | None:
        response_data = await self.make_request("POST", "/cloud", data={"url": magnet_link})
        if not response_data:
            return None

        if "requestId" not in response_data:
            if "not_available" in response_data:
                return None
            log.error("failed to add magnet to offcloud", response=response_data)
            return None
        return AddMagnetResponse(**response_data)

    async def get_user_torrent_list(self) -> list[CloudHistoryItem] | None:
        response = await self.make_request("GET", "/cloud/history")
        if not response:
            return None
        return TypeAdapter(list[CloudHistoryItem]).validate_python(response)

    async def get_torrent_info(self, request_id: str) -> CloudStatusResponse | None:
        response = await self.make_request(
            "POST", "/cloud/status", data={"requestIds": [request_id]}
        )
        if not response:
            return None

        return CloudStatusResponse(**response)

    async def get_torrent_instant_availability(
        self, magnet_links: list[str]
    ) -> CacheResponse | None:
        response = await self.make_request("POST", "/cache", data={"hashes": magnet_links})
        if not response:
            return None
        return CacheResponse(**response)

    async def get_available_torrent(self, info_hash: str) -> CloudHistoryItem | None:
        available_torrents = await self.get_user_torrent_list()
        info_hash = info_hash.casefold()
        if not available_torrents:
            return None
        for torrent in available_torrents:
            if torrent.original_link and info_hash in torrent.original_link.casefold():
                return torrent
        return None

    async def explore_folder_links(self, request_id: str) -> list[str] | None:
        response = await self.make_request("GET", f"/cloud/explore/{request_id}")
        if not response:
            return None

        return TypeAdapter(list[str]).validate_python(response)

    async def create_download_link(
        self,
        request_id: str,
        torrent_info: TorrentInfo,
        season: int = 0,
        episode: int = 0,
    ) -> str | None:
        if not torrent_info.is_directory:
            return f"https://{torrent_info.server}.offcloud.com/cloud/download/{request_id}/{torrent_info.file_name}"

        response = await self.explore_folder_links(request_id)
        if not response:
            return None
        for link in response:
            if not is_video(link):
                continue

            if not season and not episode:
                return link

            if season and episode:
                meta = TorrentMeta.parse_title(link.split("/")[-1])
                if season in meta.season and episode in meta.episode:
                    return link
        return None

    async def get_stream_link(
        self,
        info_hash: str,
        season: int,
        episode: int,
    ) -> StreamLink | None:
        magnet_resp = await self.add_magent_link(magnet.make_magnet_link(info_hash))
        if not magnet_resp:
            return None
        if magnet_resp.status != "downloaded":
            log.error("magnet is not downloaded", magnet_resp=magnet_resp)
            return None

        torrent_info = await self.get_torrent_info(magnet_resp.request_id)
        if not torrent_info:
            log.error("failed to get torrent info", magnet_resp=magnet_resp)
            return None

        download_link = await self.create_download_link(
            magnet_resp.request_id,
            torrent_info.status,
            season,
            episode,
        )
        if not download_link:
            log.debug("failed to create download link", magnet_resp=magnet_resp)
            return None
        return StreamLink(
            url=download_link,
            name=torrent_info.status.file_name,
            size=0,
        )

    # implement DebridService
    def shared_cache(self) -> bool:
        return False

    def short_name(self) -> str:
        return "offcloud"

    def name(self) -> str:
        return "OffCloud"

    def id(self) -> str:
        return "offcloud"

    async def get_stream_links(
        self,
        torrents: list[str],
        stop: asyncio.Event,
        max_results: int,
        season: int = 0,
        episode: int = 0,
    ) -> AsyncGenerator[StreamLink, None]:
        available_torrents = await self.get_torrent_instant_availability(torrents)
        if not available_torrents or len(available_torrents.cached_items) == 0:
            log.debug("no available torrents")
            return

        i = 0
        for torrent in available_torrents.cached_items:
            if not torrent:
                continue
            if link := await self.get_stream_link(
                info_hash=torrent, season=season, episode=episode
            ):
                yield link
                i += 1
                if i >= max_results:
                    return
            if stop.is_set():
                return
