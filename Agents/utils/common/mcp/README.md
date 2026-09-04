# Exa Web MCP
Build this independent stdio MCP for Agent Fleet's existing interface:
```bash
python3 -m zipapp Agents/utils/common/mcp/exa -o /data/exa-web-mcp.pyz
```
Configure the existing S3 staging and Claude integration:
```bash
export HARBOR_CC_WEB_MCP_SOURCE=/data/exa-web-mcp.pyz
export HARBOR_CC_WEB_MCP_MOUNT_PATH=/opt/agent-fleet/exa-web-mcp.pyz
export EXA_API_KEY=anonymous
```

`anonymous` satisfies the existing launcher's non-empty check but is not sent
as authentication. A real `EXA_API_KEY` enables paid Exa quota. Endpoint and
header build settings live in `exa/config.py`; never put a key there or in S3.
