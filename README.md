# MCP Service Provider for Home Assistant

This integration allows you to connect to MCP (Model Context Protocol) servers over the network (via SSE) and exposes their tools as Home Assistant services.

## Features

- **Dynamic Service Registration**: Automatically creates a service for each tool provided by the MCP server.
- **Configurable**: supports custom headers and query parameters for the SSE connection.
- **Event-driven**: Fires a `mcp_service_provider_result` event when a tool call completes.

## Installation

### Via HACS

1. Go to HACS -> Integrations.
2. Click the three dots in the top right corner and select "Custom repositories".
3. Add `https://github.com/Bluscream/mcp-service-provider` with category "Integration".
4. Search for "MCP Service Provider" and click Download.
5. Restart Home Assistant.

### Manual

1. Copy the `custom_components/mcp_service_provider` folder to your Home Assistant `custom_components` directory.
2. Restart Home Assistant.

## Configuration

1. In Home Assistant, go to Settings -> Devices & Services.
2. Click "Add Integration" and search for "MCP Service Provider".
3. Enter the URL of your MCP server (e.g., `http://192.168.1.100:8000/sse`).
4. Optionally enter headers or query parameters as JSON strings.

## Usage

Once configured, the integration will Register services under the `mcp_service_provider` domain.
The service names follow the pattern: `mcp_service_provider.<server_name>_<tool_name>`.

You can call these services from automations, scripts, or the Developer Tools.

Example result event:
```json
{
  "event_type": "mcp_service_provider_result",
  "data": {
    "entry_id": "...",
    "tool": "my_tool",
    "result": [{"type": "text", "text": "Hello from MCP!"}]
  }
}
```
