"""Azure Blob Storage's REST API, the only web requests dp-instance makes itself.

Each call is one HTTPS request to one container, signed in with a Microsoft Entra token for Azure
Storage that the Azure CLI issues for the instance's tenant. The token is shared by every thread
and read again only shortly before it expires, about hourly, so a renewal or an upload starts no
process; its own claims name the account that signs the requests. TLS trusts the system's
certificate store, as `UV_NATIVE_TLS` does for uv, and the environment's proxy settings apply,
as they do for the Azure CLI. A redirect is never followed, so the token goes only to the
account.
"""

from __future__ import annotations

import base64
import binascii
import http.client
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime
from email.utils import formatdate

import truststore

from . import processes
from .outcome import Next, Stop, failed

API_VERSION = "2023-11-03"
RESOURCE = "https://storage.azure.com/"
# A token is read again once it has less than this left, in seconds: under the five minutes in
# which the Azure CLI's token cache issues a new one instead of the one it holds.
REFRESH_BEFORE = 240
# How long a token about to expire is used before the Azure CLI is asked again.
REFRESH_AGAIN = 60
# How long the Azure CLI has to issue a token, in seconds.
TOKEN_SECONDS = 30
# The most an append block holds.
APPEND_BYTES = 4 * 1024 * 1024
# The claims that name the account a token signs in, in order of preference: a person's, then an
# application's.
NAMES = ("upn", "unique_name", "preferred_username", "appid")
# The Azure CLI's words when it has no usable sign-in for the tenant.
SIGNED_OUT = ("az login", "aadsts")


class BlobError(Exception):
    """A request Blob Storage refused, with its HTTP status and error code, or one that never got
    an answer, with neither."""

    def __init__(self, status: int | None, code: str | None, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def describe(self) -> str:
        if self.status is None:
            return self.message
        return f"{self.status} {self.code or 'no error code'}: {self.message}"


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


def issue(tenant: str) -> tuple[str, float]:
    """A token for Azure Storage in ``tenant`` from the Azure CLI, and when it expires, in
    seconds since the epoch."""
    found = processes.run(
        "az",
        "account",
        "get-access-token",
        "--resource",
        RESOURCE,
        "--tenant",
        tenant,
        "--output",
        "json",
        "--only-show-errors",
        check=False,
        timeout=TOKEN_SECONDS,
    )
    if not found.ok:
        message = f"The Azure CLI couldn't issue a token for Azure Storage: {found.first_error_line()}"
        if any(word in (found.stderr + found.stdout).lower() for word in SIGNED_OUT):
            command = f"az login --tenant {tenant}"
            raise Stop(
                "sign-in",
                message,
                reason="sign-in",
                sign_in={"command": command},
                next=(Next(command, "renews the sign-in, which only you can do"),),
            )
        raise failed(message)
    try:
        data = json.loads(found.stdout)
        token = data["accessToken"]
        if data.get("expires_on") is not None:
            expires = float(data["expires_on"])
        else:
            # Azure CLI before 2.54 gives only the local time.
            expires = datetime.strptime(data["expiresOn"], "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except (ValueError, KeyError, TypeError):
        raise failed("The Azure CLI's token for Azure Storage didn't read as one.") from None
    return token, expires


def identity(token: str) -> dict[str, str]:
    """The account a token signs in, by its own claims: its name and its object ID."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, binascii.Error):
        claims = None
    if not isinstance(claims, dict):
        raise failed("The Azure CLI's token for Azure Storage names no account.")
    name = next((claims[key] for key in NAMES if isinstance(claims.get(key), str) and claims[key]), None)
    oid = claims.get("oid") if isinstance(claims.get("oid"), str) else None
    if name is None and oid is None:
        raise failed("The Azure CLI's token for Azure Storage names no account.")
    return {"who": name or oid, **({"oid": oid} if oid else {})}


class Token:
    """The Azure CLI's token for Azure Storage in one tenant, for every thread."""

    def __init__(self, tenant: str):
        self.tenant = tenant
        self._lock = threading.Lock()
        self._value: str | None = None
        self._expires = 0.0
        self._again = 0.0

    def value(self) -> str:
        with self._lock:
            now = time.time()
            if self._value is None or (self._expires - now < REFRESH_BEFORE and now >= self._again):
                self._value, self._expires = issue(self.tenant)
                self._again = time.time() + REFRESH_AGAIN
            return self._value

    def identity(self) -> dict[str, str]:
        """The account the token signs in."""
        return identity(self.value())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is answered as it came: following it would send the token where it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener: urllib.request.OpenerDirector | None = None


def _open() -> urllib.request.OpenerDirector:
    global _opener
    if _opener is None:
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        _opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirect())
    return _opener


def send(method: str, url: str, headers: dict[str, str], body: bytes, timeout: float) -> Response:
    """One request and its answer, whatever its status; a request that gets no answer raises."""
    request = urllib.request.Request(
        url, data=body if method == "PUT" else None, headers=headers, method=method
    )
    try:
        with _open().open(request, timeout=timeout) as answer:
            return Response(answer.status, {k.lower(): v for k, v in answer.headers.items()}, answer.read())
    except urllib.error.HTTPError as error:
        try:
            return Response(error.code, {k.lower(): v for k, v in error.headers.items()}, error.read())
        finally:
            error.close()
    except urllib.error.URLError as error:
        raise BlobError(
            None, None, f"{urllib.parse.urlsplit(url).netloc} didn't answer: {error.reason}"
        ) from None
    except (OSError, http.client.HTTPException) as error:
        raise BlobError(None, None, f"{urllib.parse.urlsplit(url).netloc} didn't answer: {error}") from None


def _error(response: Response) -> BlobError:
    """A refusal, by its error code and the first line of its message."""
    code = response.header("x-ms-error-code")
    text = ""
    if response.body:
        try:
            found = ElementTree.fromstring(response.body)
            code = code or found.findtext("Code")
            text = found.findtext("Message") or ""
        except ElementTree.ParseError:
            text = response.body.decode("utf-8", "replace")
    first = next((line.strip() for line in text.splitlines() if line.strip()), "no message")
    return BlobError(response.status, code, first)


class Container:
    """One container in a storage account."""

    def __init__(self, account: str, name: str, token: Token):
        self.account = account
        self.name = name
        self.token = token
        self.url = f"https://{account}.blob.core.windows.net/{name}"

    def request(
        self,
        method: str,
        blob: str = "",
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        timeout: float = 30,
    ) -> Response:
        url = self.url + (f"/{urllib.parse.quote(blob)}" if blob else "")
        if query:
            url += "?" + urllib.parse.urlencode(query)
        sent = {
            "x-ms-version": API_VERSION,
            "x-ms-date": formatdate(usegmt=True),
            "Authorization": f"Bearer {self.token.value()}",
            **(headers or {}),
        }
        response = send(method, url, sent, body, timeout)
        if response.status >= 300:
            raise _error(response)
        return response

    # The container ---------------------------------------------------------------------

    def metadata(self) -> dict[str, str] | None:
        """The container's metadata; None when there's no such container."""
        try:
            found = self.request("GET", query={"restype": "container"})
        except BlobError as error:
            if error.code == "ContainerNotFound":
                return None
            raise
        return {
            key[len("x-ms-meta-") :]: value
            for key, value in found.headers.items()
            if key.startswith("x-ms-meta-")
        }

    def create(self, metadata: dict[str, str]) -> None:
        """Create the container with ``metadata``; one that exists already is left as it is."""
        try:
            self.request(
                "PUT",
                query={"restype": "container"},
                headers={f"x-ms-meta-{key}": value for key, value in metadata.items()},
            )
        except BlobError as error:
            if error.code != "ContainerAlreadyExists":
                raise

    def names(self, prefix: str) -> list[str]:
        """The names of the blobs that start with ``prefix``."""
        found, marker = [], ""
        while True:
            query = {"restype": "container", "comp": "list", "prefix": prefix}
            if marker:
                query["marker"] = marker
            listing = ElementTree.fromstring(self.request("GET", query=query).body)
            found += [item.findtext("Name") for item in listing.iter("Blob")]
            marker = listing.findtext("NextMarker") or ""
            if not marker:
                return found

    # Blobs -----------------------------------------------------------------------------

    def size(self, blob: str) -> int | None:
        """How many bytes the blob holds, reading only its properties; None when there's none."""
        try:
            found = self.request("HEAD", blob)
        except BlobError as error:
            if error.status == 404:
                return None
            raise
        return int(found.header("content-length") or 0)

    def get(self, blob: str) -> Response | None:
        """The blob, with its ETag and lease state in its headers; None when there's none."""
        try:
            return self.request("GET", blob)
        except BlobError as error:
            if error.code == "BlobNotFound":
                return None
            raise

    def put(
        self,
        blob: str,
        body: bytes,
        content_type: str,
        *,
        etag: str | None = None,
        new: bool = False,
        lease: str | None = None,
        timeout: float = 30,
    ) -> str:
        """Write the blob and return its new ETag: only while it's still at ``etag``, or only when
        there's none yet with ``new``, or under ``lease``."""
        headers = {"x-ms-blob-type": "BlockBlob", "Content-Type": content_type}
        if etag is not None:
            headers["If-Match"] = etag
        if new:
            headers["If-None-Match"] = "*"
        if lease is not None:
            headers["x-ms-lease-id"] = lease
        return self.request("PUT", blob, headers=headers, body=body, timeout=timeout).header("etag") or ""

    def append(self, blob: str, data: bytes, at: int, content_type: str) -> None:
        """Append ``data``, at most ``APPEND_BYTES``, to the append blob, which holds ``at``
        bytes: a new one when ``at`` is 0. Appending creates no blob version; creating one does. A
        blob that no longer holds ``at`` bytes is refused."""
        if at == 0:
            try:
                self.request(
                    "PUT",
                    blob,
                    headers={
                        "x-ms-blob-type": "AppendBlob",
                        "Content-Type": content_type,
                        "If-None-Match": "*",
                    },
                )
            except BlobError as error:
                # An earlier attempt created it and appended nothing; one that holds anything is
                # refused by the append's position below.
                if error.code != "BlobAlreadyExists":
                    raise
        self.request(
            "PUT",
            blob,
            query={"comp": "appendblock"},
            headers={"x-ms-blob-condition-appendpos": str(at)},
            body=data,
            timeout=120,
        )

    def lease(
        self,
        blob: str,
        action: str,
        *,
        lease: str | None = None,
        proposed: str | None = None,
        seconds: int | None = None,
        etag: str | None = None,
    ) -> Response:
        """One lease call on the blob: `acquire` a new lease, ``proposed``, for ``seconds``;
        `renew` or `release` ``lease``; or `break` whatever lease it has at once, only while the
        blob is still at ``etag``."""
        headers = {"x-ms-lease-action": action}
        if lease is not None:
            headers["x-ms-lease-id"] = lease
        if proposed is not None:
            headers["x-ms-proposed-lease-id"] = proposed
        if seconds is not None:
            headers["x-ms-lease-duration"] = str(seconds)
        if action == "break":
            headers["x-ms-lease-break-period"] = "0"
        if etag is not None:
            headers["If-Match"] = etag
        return self.request("PUT", blob, query={"comp": "lease"}, headers=headers, timeout=10)
