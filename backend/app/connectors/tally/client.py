import codecs

import httpx

from app.connectors.base import ConnectorError


def wire_body(payload: bytes) -> bytes:
    """A UTF-8 XML request as Tally should receive it: UTF-16 with a byte order mark. Tally
    answers in the request's encoding, and in UTF-8 it turns text outside Latin script, such
    as names in Devanagari, into "?"."""
    return codecs.BOM_UTF16_LE + payload.decode("utf-8").encode("utf-16-le")


class TallyHttpClient:
    """Thin HTTP transport for Tally's XML server. In TallyPrime: Alt+Z (Exchange) >
    Configure > Client/Server Configuration (F1 > Settings > Connectivity in older
    releases), 'TallyPrime acts as' Both or Server, port 9000 by default."""

    def __init__(
        self,
        url: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def _unreachable(self, exc: Exception) -> ConnectorError:
        return ConnectorError(
            f"Cannot reach Tally at {self.url}. Make sure TallyPrime is running with a company "
            f"open and its HTTP/XML server enabled on this port. ({exc.__class__.__name__})"
        )

    def ping(self) -> str:
        try:
            resp = self._client.get(self.url)
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        return resp.text.strip()

    def post(self, payload: bytes) -> bytes:
        try:
            resp = self._client.post(
                self.url,
                content=wire_body(payload),
                # Without the charset, Tally reads a UTF-16 body as an "Unknown Request".
                headers={"Content-Type": "text/xml; charset=utf-16"},
            )
        except httpx.TimeoutException as exc:
            raise ConnectorError(
                f"Tally at {self.url} did not respond in time. A dialog may be open in Tally."
            ) from exc
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if resp.status_code >= 400:
            raise ConnectorError(f"Tally returned HTTP {resp.status_code}")
        return resp.content

    def close(self) -> None:
        self._client.close()
