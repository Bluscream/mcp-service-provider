"""The MCP Service Provider integration."""
from __future__ import annotations

import logging
import json
import asyncio
from contextlib import asynccontextmanager
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv, httpx_client
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify

import voluptuous as vol
import httpx
import uuid
from urllib.parse import urlencode, urlparse, urlunparse
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

from .const import DOMAIN, CONF_URL, CONF_HEADERS, CONF_QUERY_PARAMS

_LOGGER = logging.getLogger(__name__)

class NonClosingAsyncClient(httpx.AsyncClient):
    """An AsyncClient that does not close on __aexit__."""
    async def __aexit__(self, *args: Any) -> None:
        """Do nothing on exit."""
        pass

def _get_mcp_httpx_client(hass: HomeAssistant, **kwargs: Any) -> httpx.AsyncClient:
    """Get a non-closing httpx client from Home Assistant."""
    client = httpx_client.get_async_client(hass)
    # Return a wrapper that identifies as the same but won't close
    # Use headers/timeout from kwargs if provided
    if kwargs.get("headers"):
        client.headers.update(kwargs["headers"])
    if kwargs.get("timeout"):
        client.timeout = kwargs["timeout"]
    
    # We create a new client using the same transport to avoid blocking SSL init
    return httpx.AsyncClient(
        transport=client._transport,
        headers=kwargs.get("headers"),
        timeout=kwargs.get("timeout"),
        auth=kwargs.get("auth")
    )

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the MCP Service Provider component."""
    hass.data[DOMAIN] = {}
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MCP Service Provider from a config entry."""
    url = entry.data[CONF_URL]
    headers_json = entry.data.get(CONF_HEADERS, "{}")
    query_params_json = entry.data.get(CONF_QUERY_PARAMS, "{}")

    try:
        headers = json.loads(headers_json)
        query_params = json.loads(query_params_json)
    except ValueError as err:
        _LOGGER.error("Invalid JSON in headers or query params: %s", err)
        return False

    # Ensure Mcp-Session-Id exists as some servers require it
    if "Mcp-Session-Id" not in headers:
        headers["Mcp-Session-Id"] = str(uuid.uuid4())

    # Store entry data
    hass.data[DOMAIN][entry.entry_id] = {
        "url": url,
        "headers": headers,
        "params": query_params,
        "services": [],
        "session": None,
        "lock": asyncio.Lock()
    }

    # Connect and discover tools
    try:
        # We use a short-lived connection to discover tools initially
        # Or we can maintain a persistent one. Let's try to list tools first.
        # Note: SSE client might need to be kept alive.
        # For now, let's just list and register.
        await _discover_and_register_services(hass, entry)
    except Exception as err:
        _LOGGER.exception("Error connecting to MCP server %s: %s", url, err)
        return False

    return True

@asynccontextmanager
async def _async_mcp_client(hass: HomeAssistant, url: str, headers: dict, params: dict):
    """Attempt to connect to MCP via Streamable HTTP (modern) or SSE (classic)."""
    # Ensure URL ends with a trailing slash (required by ipc-mcp server)
    if not url.endswith('/'):
        url = url + '/'

    # Extract token from Authorization header and ensure it's in query params for both transports
    token = None
    auth_header = headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if token:
        # Ensure token is present in params dict
        params = dict(params) if isinstance(params, dict) else {}
        params.setdefault("token", token)

    # Build final URL with query parameters (including token if present)
    if params:
        url_parts = list(urlparse(url))
        query = urlencode(params)
        if url_parts[4]:  # existing query string
            url_parts[4] += "&" + query
        else:
            url_parts[4] = query
        url = urlunparse(url_parts)

    # Try Streamable HTTP first (modern IPC-MCP uses this transport)
    try:
        client = mcp_httpx_factory(headers=headers)
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            yield read, write
    except Exception as err:
        _LOGGER.debug("Streamable HTTP failed (%s), falling back to SSE", err)
        # For SSE the server expects the token in the query string, not header
        token = None
        auth_header = headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()
        # Append token to query if we have one and it's not already present
        if token:
            url_parts = list(urlparse(url))
            query_dict = dict([pair.split("=") for pair in url_parts[4].split("&") if pair]) if url_parts[4] else {}
            query_dict.setdefault("token", token)
            url_parts[4] = urlencode(query_dict)
            url = urlunparse(url_parts)
        async with sse_client(url, headers=headers, httpx_client_factory=mcp_httpx_factory) as (read, write):
            yield read, write

async def _discover_and_register_services(hass: HomeAssistant, entry: ConfigEntry):
    """Discover tools and register them as services."""
    data = hass.data[DOMAIN][entry.entry_id]
    url = data["url"]
    headers = data["headers"]
    params = data["params"]

    async with _async_mcp_client(hass, url, headers, params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            response = await session.list_tools()
            tools = response.tools

            prefix = slugify(entry.title)

            for tool in tools:
                service_name = f"{prefix}_{slugify(tool.name)}"
                schema = _convert_schema_to_voluptuous(tool.inputSchema)

                # Use a closure to capture variables
                def make_handler(t_name):
                    async def handle_service(call: ServiceCall):
                        """Handle the service call."""
                        await _call_mcp_tool(hass, entry, t_name, call.data)
                    return handle_service

                hass.services.async_register(
                    DOMAIN, service_name, make_handler(tool.name), schema=schema
                )
                
                # Try to register service description if available
                if tool.description:
                    pass 

                data["services"].append(service_name)
                _LOGGER.info("Registered service: %s.%s - %s", DOMAIN, service_name, tool.description)

async def _call_mcp_tool(hass: HomeAssistant, entry: ConfigEntry, tool_name: str, arguments: dict):
    """Call an MCP tool."""
    data = hass.data[DOMAIN][entry.entry_id]
    url = data["url"]
    headers = data["headers"]
    params = data["params"]

    async with _async_mcp_client(hass, url, headers, params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            _LOGGER.info("MCP Tool result: %s", result)
            
            # Fire event with result
            hass.bus.async_fire(
                f"{DOMAIN}_result",
                {
                    "entry_id": entry.entry_id,
                    "tool": tool_name,
                    "result": [content.model_dump() if hasattr(content, "model_dump") else str(content) for content in result.content]
                }
            )

def _convert_schema_to_voluptuous(schema: dict):
    """Converts a JSON Schema tool input to a Voluptuous schema."""
    if not schema or "properties" not in schema:
        return vol.Schema({})

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    vol_schema_dict = {}
    for name, prop in properties.items():
        # Map types
        prop_type = prop.get("type")
        val = str
        if prop_type == "integer":
            val = int
        elif prop_type == "number":
            val = float
        elif prop_type == "boolean":
            val = bool
        elif prop_type == "object":
            val = dict
        elif prop_type == "array":
            val = list

        if name in required:
            vol_schema_dict[vol.Required(name)] = val
        else:
            vol_schema_dict[vol.Optional(name)] = val

    return vol.Schema(vol_schema_dict)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data:
        for service in data["services"]:
            hass.services.async_remove(DOMAIN, service)
        hass.data[DOMAIN].pop(entry.entry_id)
    return True
