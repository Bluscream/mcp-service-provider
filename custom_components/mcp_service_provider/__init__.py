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
from homeassistant.helpers.service import async_set_service_schema
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

from .const import DOMAIN, CONF_URL, CONF_HEADERS, CONF_QUERY_PARAMS, CONF_NAME

_LOGGER = logging.getLogger(__name__)

class NonClosingAsyncClient(httpx.AsyncClient):
    """An AsyncClient that does not close on __aexit__."""
    async def __aexit__(self, *args: Any) -> None:
        """Do nothing on exit."""
        pass

def _get_mcp_httpx_client(hass: HomeAssistant, *, headers: dict | None = None, **kwargs: Any) -> httpx.AsyncClient:
    """Get a non‑closing httpx client from Home Assistant, merging custom headers.
    """
    base = httpx_client.get_async_client(hass)
    # Start with the base client’s default headers
    merged_headers = dict(base.headers)
    # Ensure mandatory headers for some MCP servers (especially .NET based like ipc-mcp)
    merged_headers.update({
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json"
    })
    # Merge any explicit headers passed via the function call
    if headers:
        merged_headers.update(headers)
    if kwargs.get("headers"):
        merged_headers.update(kwargs["headers"])
    # Build a new client that re-uses the transport but carries the merged headers
    return httpx.AsyncClient(
        transport=base._transport,
        headers=merged_headers,
        timeout=kwargs.get("timeout") or 30.0,
        auth=kwargs.get("auth"),
    )

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the MCP Service Provider component."""
    hass.data[DOMAIN] = {}
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MCP Service Provider from a config entry."""
    _LOGGER.debug("async_setup_entry called for entry %s with URL %s", entry.entry_id, entry.data.get(CONF_URL))
    url = entry.data[CONF_URL]
    headers_json = entry.data.get(CONF_HEADERS, "{}")
    query_params_json = entry.data.get(CONF_QUERY_PARAMS, "{}")

    try:
        headers = json.loads(headers_json)
        query_params = json.loads(query_params_json)
    except ValueError as err:
        _LOGGER.error("Invalid JSON in headers or query params: %s", err)
        return False

    # Manual Mcp-Session-Id is NOT needed for initial setup;
    # it is assigned or managed by the transport library (Streamable HTTP or SSE).
    # Removed manual uuid.uuid4() generation.

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
    _LOGGER.debug("Attempting to discover and register services for entry %s", entry.entry_id)
    try:
        # We use a short-lived connection to discover tools initially
        # Or we can maintain a persistent one. Let's try to list tools first.
        # Note: SSE client might need to be kept alive.
        # For now, let's just list and register.
        await _discover_and_register_services(hass, entry)
    except Exception as err:
        _LOGGER.exception("Error connecting to MCP server %s: %s", url, err)
        _LOGGER.debug("Failed to discover services for entry %s", entry.entry_id)
        return False

    return True

@asynccontextmanager
async def _async_mcp_client(hass: HomeAssistant, url: str, headers: dict, params: dict):
    """Attempt to connect to MCP via Streamable HTTP (modern) or SSE (classic)."""
    _LOGGER.debug("_async_mcp_client invoked for URL %s", url)

    # Use httpx.URL for cleaner URL manipulation
    import httpx
    target_url = httpx.URL(url)
    
    # Ensure the path ends with a slash (required by ipc-mcp server normalization)
    if not target_url.path.endswith("/"):
        new_path = target_url.path + "/"
        target_url = target_url.copy_with(path=new_path)
    
    # We no longer add the token to query parameters for security reasons.
    # The token is handled via the 'Authorization: Bearer <token>' header
    # which is merged into the httpx client in _get_mcp_httpx_client.
    
    final_url = str(target_url)
    _LOGGER.debug("_async_mcp_client: final target URL: %s", final_url)

    # Try Streamable HTTP first (modern IPC-MCP uses this transport)
    try:
        # In this modern transport, we let the library manage the session ID.
        # We also MUST have the correct Accept and Content-Type headers for .NET servers.
        client = _get_mcp_httpx_client(hass, headers=headers)
        _LOGGER.debug("_async_mcp_client: attempting Streamable HTTP with %s", final_url)
        # Call without manual session_id to let the transport library assign/manage it
        async with streamable_http_client(final_url, http_client=client) as (read, write, _):
            yield read, write
    except Exception as err:
        _LOGGER.debug("Streamable HTTP failed (%s), falling back to SSE", err)
        _LOGGER.debug("_async_mcp_client: Streamable HTTP failed, falling back to SSE for %s", final_url)
        
        async with sse_client(final_url, headers=headers, httpx_client_factory=lambda **kwargs: _get_mcp_httpx_client(hass, **kwargs)) as (read, write):
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
            _LOGGER.debug("Discovered %d tools from MCP server", len(tools))

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

                # Register service description for better UI visibility
                fields_metadata = {}
                properties = tool.inputSchema.get("properties", {})
                required_fields = tool.inputSchema.get("required", [])
                
                for prop_name, prop_info in properties.items():
                    field_desc = prop_info.get("description", "")
                    field_default = prop_info.get("default")
                    
                    # Create selector based on type
                    prop_type = prop_info.get("type")
                    selector = {"text": {}} # Default
                    if prop_type == "boolean":
                        selector = {"boolean": {}}
                    elif prop_type == "integer" or prop_type == "number":
                        selector = {"number": {"mode": "box"}}
                    elif prop_type == "object" or prop_type == "array":
                        selector = {"object": {}}
                    
                    fields_metadata[prop_name] = {
                        "name": prop_name.replace("_", " ").title(),
                        "description": field_desc,
                        "required": prop_name in required_fields,
                        "selector": selector
                    }
                    if field_default is not None:
                        fields_metadata[prop_name]["default"] = field_default
                    
                    # Add an example if possible to help the UI
                    if prop_type == "string":
                        fields_metadata[prop_name]["example"] = "example_value"

                hass.services.async_register(
                    DOMAIN, service_name, make_handler(tool.name), schema=schema
                )
                
                # Set service description for Home Assistant UI using the correct helper
                async_set_service_schema(
                    hass, 
                    DOMAIN, 
                    service_name, 
                    {
                        "description": tool.description or f"Call MCP tool {tool.name}",
                        "fields": fields_metadata
                    }
                )

                data["services"].append(service_name)
                _LOGGER.info("Registered service: %s.%s - %s", DOMAIN, service_name, tool.description)

        # Register a generic tool-calling service
        async def async_call_tool(call: ServiceCall):
            tool_name = call.data.get("tool_name")
            arguments = call.data.get("arguments", {})
            await _call_mcp_tool(hass, entry, tool_name, arguments)
            
        hass.services.async_register(
            DOMAIN, 
            "call_tool", 
            async_call_tool,
            schema=vol.Schema({
                vol.Required("tool_name"): cv.string,
                vol.Optional("arguments"): dict
            })
        )

async def _call_mcp_tool(hass: HomeAssistant, entry: ConfigEntry, tool_name: str, arguments: dict):
    """Call an MCP tool."""
    _LOGGER.debug("Calling MCP tool %s with arguments %s for entry %s", tool_name, arguments, entry.entry_id)
    data = hass.data[DOMAIN][entry.entry_id]
    url = data["url"]
    headers = data["headers"]
    params = data["params"]

    async with _async_mcp_client(hass, url, headers, params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            _LOGGER.info("MCP Tool result: %s", result)
            _LOGGER.debug("MCP tool %s completed, result length: %d", tool_name, len(result.content) if hasattr(result, 'content') else 0)
            
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
        # Map types to Home Assistant validators
        prop_type = prop.get("type")
        val: Any = cv.string
        if prop_type == "integer":
            val = vol.Coerce(int)
        elif prop_type == "number":
            val = vol.Coerce(float)
        elif prop_type == "boolean":
            val = cv.boolean
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
