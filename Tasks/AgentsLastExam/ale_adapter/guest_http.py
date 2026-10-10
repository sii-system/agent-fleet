"""Forward one CUA HTTP request inside a prepared ALE Linux sandbox."""

import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main():
    request_file, output = sys.argv[1:]
    request = json.loads(Path(request_file).read_text())
    req = urllib.request.Request(request["url"], method=request["method"],
        data=base64.b64decode(request["body"]) if request["body"] else None,
        headers=request["headers"])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=request["timeout"])
    except urllib.error.HTTPError as error:
        response = error
    with response:
        if "text/event-stream" in response.headers.get("Content-Type", ""):
            lines, data_seen = [], False
            while line := response.readline():
                lines.append(line)
                data_seen = data_seen or line.startswith(b"data:")
                if data_seen and not line.strip():
                    break
            body = b"".join(lines)
        else:
            body = response.read()
        Path(output + ".body").write_bytes(body)
        Path(output + ".json").write_text(json.dumps({"status": response.code,
            "content_type": response.headers.get("Content-Type", "application/octet-stream")}))


if __name__ == "__main__":
    main()
