#!/usr/bin/env python
#
# A library that provides a Python interface to the Telegram Bot API
# Copyright (C) 2015-2021
# Leandro Toledo de Souza <devs@python-telegram-bot.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser Public License for more details.
#
# You should have received a copy of the GNU Lesser Public License
# along with this program.  If not, see [http://www.gnu.org/licenses/].
"""This module contains an abstract class to make POST and GET requests."""
import abc
from pathlib import Path
from types import TracebackType
from typing import Union, Optional, Tuple, Dict, Awaitable, Type

try:
    import ujson as json
except ImportError:
    import json  # type: ignore[no-redef]

from telegram._version import __version__ as ptb_ver

# pylint: disable=ungrouped-imports
from telegram import InputFile, InputMedia
from telegram.error import (
    TelegramError,
    BadRequest,
    ChatMigrated,
    Conflict,
    InvalidToken,
    NetworkError,
    RetryAfter,
    Unauthorized,
)
from telegram._utils.types import JSONDict, FilePathInput


class PtbRequestBase(abc.ABC):
    """Abstract base class for python-telegram-bot which provides simple means to work with
    different async HTTP libraries.

    """

    __slots__ = ('_connect_timeout', '_con_pool_size', '_con_pool')

    user_agent = (
        f'Python Telegram Bot {ptb_ver}'
        f' (https://github.com/python-telegram-bot/python-telegram-bot)'
    )

    async def __aenter__(self) -> 'PtbRequestBase':
        await self.do_init()
        return self

    async def __aexit__(
        self, exc_type: Type[Exception], exc_val: Exception, exc_tb: TracebackType
    ) -> None:
        await self.stop()

    @abc.abstractmethod
    async def do_init(self) -> None:
        """Initialize resources used by this class."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop & clear resources used by this class."""

    async def post(
        self, url: str, data: Optional[JSONDict], timeout: float = None
    ) -> Union[JSONDict, bool]:
        """Request an URL.

        Args:
            url (:obj:`str`): The web location we want to retrieve.
            data (Dict[:obj:`str`, :obj:`str` | :obj:`int`], optional): A dict of key/value pairs.
            timeout (:obj:`int` | :obj:`float`, optional): If this value is specified, use it as
                the read timeout from the server (instead of the one specified during creation of
                the connection pool).

        Returns:
          A JSON object.

        """
        # Optional files to upload in multi-part form.
        files = {}

        # Convert data into a JSON serializable object which we can send to telegram servers.
        # TODO p3: We should implement a proper Serializer instead of all this memcopy &
        #          manipulations.
        # pylint: disable=too-many-nested-blocks
        for key, val in data.copy().items():
            if isinstance(val, InputFile):
                files[key] = val.field_tuple
                del data[key]
            elif isinstance(val, (float, int)):
                # TODO p3: Is this really necessary? Seems like an ancient relic.
                data[key] = str(val)
            elif key == 'media':
                # List of media
                if isinstance(val, list):
                    # Attach and set val to attached name for all
                    media = []
                    for med in val:
                        media_dict = med.to_dict()
                        media.append(media_dict)
                        if isinstance(med.media, InputFile):
                            files[med.media.attach] = med.media.field_tuple
                            # med.media = None
                            # if the file has a thumb, we also need to attach it to the data
                            if "thumb" in media_dict:
                                files[med.thumb.attach] = med.thumb.field_tuple
                                # med.thumb = None
                    data[key] = json.dumps(media)
                # Single media
                else:
                    # Attach and set val to attached name
                    media_dict = val.to_dict()
                    if isinstance(val.media, InputFile):
                        files[val.media.attach] = val.media.field_tuple

                        # if the file has a thumb, we also need to attach it to the data
                        if "thumb" in media_dict:
                            files[val.thumb.attach] = val.thumb.field_tuple
                    data[key] = json.dumps(media_dict)
            elif isinstance(val, list):
                # In case we're sending files, we need to json-dump lists manually
                # As we can't know if that's the case, we just json-dump here
                data[key] = json.dumps(val)

        result = await self._request_wrapper(
            method='POST', url=url, data=data, files=files, read_timeout=timeout
        )
        return self._parse(result)

    async def retrieve(self, url: str, timeout: float = None) -> bytes:
        """Retrieve the contents of a file by its URL.

        Args:
            url (:obj:`str`): The web location we want to retrieve.
            timeout (:obj:`int` | :obj:`float`): If this value is specified, use it as the read
                timeout from the server (instead of the one specified during creation of the
                connection pool).

        Raises:
            TelegramError

        """
        return await self._request_wrapper('GET', url, None, {}, read_timeout=timeout)

    async def download(self, url: str, filepath: FilePathInput, timeout: float = None) -> None:
        """Download a file from the given ``url`` and save it to ``filename``.

        Args:
            url (:obj:`str`): The web location we want to retrieve.
            filepath (:obj:`pathlib.Path` | :obj:`str`): The filepath to download the file to.
            timeout (:obj:`int` | :obj:`float`, optional): If this value is specified, use it as
                the read timeout from the server (instead of the one specified during creation of
                the connection pool).

        .. versionchanged:: 14.0
            The ``filepath`` parameter now also accepts :obj:`pathlib.Path` objects as argument.

        Raises:
            TelegramError

        """
        Path(filepath).write_bytes(await self.retrieve(url, timeout))

    async def _request_wrapper(
        self,
        method: str,
        url: str,
        data: Optional[JSONDict],
        files: Dict[str, Tuple[str, bytes, str]],
        read_timeout: float = None,
    ) -> bytes:
        """Wraps the real implementation request method.

        Performs the following tasks:
        * Handle the various HTTP response codes.
        * Parse the Telegram server response.

        Args:
            method: HTTP method (i.e. 'POST', 'GET', etc.).
            url: The request's URL.
            data: Data to send over as the request's payload.
            files: Files to upload as multi-form. Key is the form field name. Value is the file to
                   upload (filename, file-content, content-type).
            read_timeout: Timeout for waiting to server's response.

        Returns:
            bytes: The payload part of the HTTP server response.

        Raises:
            TelegramError

        """
        try:
            code, payload = await self.do_request(
                method, url, data, files, read_timeout=read_timeout
            )
        except TelegramError:
            raise
        except Exception as err:
            raise NetworkError(f"Unknown error in HTTP implementation {err}") from err

        if 200 <= code <= 299:
            # 200-299 range are HTTP success statuses
            return payload

        try:
            message = str(self._parse(payload))
        except ValueError:
            message = 'Unknown HTTPError'

        if code in (401, 403):
            raise Unauthorized(message)
        if code == 400:
            raise BadRequest(message)
        if code == 404:
            raise InvalidToken()
        if code == 409:
            raise Conflict(message)
        if code == 413:
            raise NetworkError(
                'File too large. Check telegram api limits '
                'https://core.telegram.org/bots/api#senddocument'
            )
        if code == 502:
            raise NetworkError('Bad Gateway')
        raise NetworkError(f'{message} ({code})')

    @staticmethod
    def _parse(json_data: bytes) -> Union[JSONDict, bool]:
        """Try and parse the JSON returned from Telegram.

        Returns:
            dict: A JSON parsed as Python dict with results - on error this dict will be empty.

        """
        decoded_s = json_data.decode('utf-8', 'replace')
        try:
            data = json.loads(decoded_s)
        except ValueError as exc:
            raise TelegramError('Invalid server response') from exc

        if not data.get('ok'):  # pragma: no cover
            description = data.get('description')
            parameters = data.get('parameters')
            if parameters:
                migrate_to_chat_id = parameters.get('migrate_to_chat_id')
                if migrate_to_chat_id:
                    raise ChatMigrated(migrate_to_chat_id)
                retry_after = parameters.get('retry_after')
                if retry_after:
                    raise RetryAfter(retry_after)
            if description:
                return description

        return data['result']

    @abc.abstractmethod
    async def do_request(
        self,
        method: str,
        url: str,
        data: JSONDict,
        files: Dict[str, Tuple[str, bytes, str]],
        read_timeout: float = None,
        write_timeout: float = None,
    ) -> Tuple[int, bytes]:
        """Implement this method using the real HTTP client.

        Args:
            method: HTTP method (i.e. 'POST', 'GET', etc.).
            url: The request's URL.
            data: Data to send over as the request's payload.
            files: Files to upload as multi-form. Key is the form field name. Value is the file to
                   upload (filename, file-content, content-type).
            read_timeout: Timeout for waiting to server's response.
            write_timeout: Timeout for sending data to the server.

        Returns:
            Tuple of the HTTP return code & the payload part of the server response.

        """
