#
# coding: utf-8
# Copyright (c) 2018 DATADVANCE
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""GraphQL client application."""


import asyncio
import textwrap
import time
import uuid

from .transport import GraphqlWsTransport


class GraphqlWsClient:
    """A client for the GraphQL WebSocket server.

    The client implements a WebSocket-based GraphQL protocol. It is the
    same protocol as the server implemented by the `GraphqlWsConsumer`
    class. See its docstring for details and for the protocol
    description.

    This class implements only the protocol itself. The implementation
    of the message delivery extracted into the separate interface
    `GraphqlWsTransport`. So it is possible to use this client with
    different network frameworks (e.g. Tornado, AIOHTTP).

    Note that `gql_receive` method retrieves the first response received
    by backend, when used with subscriptions it may either return
    subscription data or some query result. The response type must be
    checked outside client manually.

    Args:
        transport: The `GraphqlWsTransport` instance used to send and
            receive messages over the WebSocket connection.
    """

    def __init__(self, transport):
        assert isinstance(
            transport, GraphqlWsTransport
        ), "The 'transport' must implement the 'GraphqlWsTransport' interface!"
        self._transport = transport

    # Increase default timeout for websocket messages to avoid errors on
    # slow machines.
    TIMEOUT = GraphqlWsTransport.TIMEOUT

    @staticmethod
    def _is_keep_alive_response(response):
        """Check if received GraphQL response is keep-alive message."""
        return response.get("type") == "ka"

    @staticmethod
    def _response_payload(response):
        """Retrieve payload from the response or raise error."""
        payload = response.get("payload", None)
        if payload is not None and "errors" in payload:
            message = f"Response contains errors!\n{response}"
            raise GraphqlWsResponseError(message, payload)
        return payload

    async def gql_connect_and_init(self, connect_only=False):
        """Establish and initialize WebSocket GraphQL connection.

        1. Establish WebSocket connection.
        2. Initialize GraphQL connection. Skipped if connect_only=True.
        """

        await self._transport.connect(timeout=self.TIMEOUT)
        if not connect_only:
            await self._transport.send({"type": "connection_init", "payload": ""})
            resp = await self._transport.receive()
            assert resp["type"] == "connection_ack", f"Unexpected response `{resp}`!"

    # Default value for `id`, because `None` is also a valid value.
    AUTO = object()

    async def gql_send(self, *, id=AUTO, type=None, payload=None):
        """Send GraphQL message.

        If any argument is `None` it is excluded from the message.

        Args:
            id: The message identifier. Automatically generated by
                default.
            type: The message type.
            payload: The payload dict.
        Returns:
            The message identifier.
        """

        if id is self.AUTO:
            id = str(uuid.uuid4().hex)
        message = {}
        message.update({"id": id} if id is not None else {})
        message.update({"type": type} if type is not None else {})
        message.update({"payload": payload} if payload is not None else {})
        await self._transport.send(message)
        return id

    async def _next_response(self, wait_id=None):
        """Fetch next data response, optionally wait for the response
        with the given id.
        """

        while True:
            response = await self._transport.receive()
            if self._is_keep_alive_response(response):
                continue
            if wait_id is None or response["id"] == wait_id:
                return response

    async def gql_receive(self, *, wait_id=None):
        """Receive GraphQL message checking its content.

        Args:
            wait_id: Wait until response with the given id received, all
                intermediate responses will be skipped.
        Returns:
            The `payload` field of the message received or `None`.
        """
        response = await self._next_response(wait_id=wait_id)
        return self._response_payload(response)

    async def gql_execute(self, query, variables=None):
        """Execute query or mutation request and wait until reply for
        the query is received.

        Args:
            query: A GraphQL string query. We `dedent` it, so you do not
                have to.
            variables: Dict of variables (optional).
        Returns:
            Dictionary with the GraphQL response.
        """

        id = await self.gql_start(query, variables=variables)
        resp = await self.gql_receive(wait_id=id)
        # Consume 'complete' message.
        await self.gql_receive(wait_id=id)
        return resp

    async def gql_subscribe(self, query, *, variables=None, wait_confirmation=True):
        """Execute subscription request and wait for confirmation.

        Args:
            query: A GraphQL string query. We `dedent` it, so you do not
                have to.
            variables: Dict of variables (optional).
            wait_confirmation: If `True` wait until subscription
                confirmation message received.

        Returns:
            The message identifier.
        """
        id = await self.gql_start(query, variables=variables)
        if wait_confirmation:
            await self.gql_receive(wait_id=id)
        return id

    async def gql_start(self, query, *, variables=None):
        """Start GraphQL request. Responses must be checked explicitly.

        Args:
            query: A GraphQL string query. We `dedent` it, so you do not
                have to.
            variables: Dict of variables (optional).

        Returns:
            The message identifier.
        """

        return await self.gql_send(
            type="start",
            payload={"query": textwrap.dedent(query), "variables": variables or {}},
        )

    async def gql_finalize(self):
        """Disconnect and wait the application to finish gracefully."""
        await self._transport.shutdown()

    async def gql_wait_response(self, response_checker, timeout=None):
        """Wait for particular response from GraphQL backend and skip
        all intermediate responses.

        Useful when you need to need to wait until subscription reports
        desired state or skip subscription messages between request and
        response.

        Args:
            response_checker: Function with accepts GraphQL response as
                single parameter and must return `True` for desired
                response and `False` the other responses.
            timeout: Seconds to wait until response is received.

        Returns:
            Response payload, same as `gql_receive`.

        Raises:
            `asyncio.TimeoutError` when timeout is reached.
        """

        if timeout is None:
            timeout = self.TIMEOUT
        while timeout > 0:
            start = time.monotonic()
            try:
                response = await self.gql_receive()
                if response_checker(response):
                    return response
            except asyncio.TimeoutError:
                # Ignore `gql_receive` calls timeout until wait timeout
                # is reached.
                pass
            timeout -= time.monotonic() - start
        raise asyncio.TimeoutError


class GraphqlWsResponseError(Exception):
    """Errors data from the GraphQL response."""

    def __init__(self, message, response):
        super().__init__(self, message)
        assert "errors" in response, "Response must contain errors!"
        self.response = response
        self.errors = response["errors"]
