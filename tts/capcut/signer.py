"""Pure Python cryptography and request signing for CapCut TTS API.

Ported from the user's trans-zh project (src/zhsub/dub/capcut/signer.py).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from typing import Any

from .config import TTS_SIGN_PUBLIC_KEY_PEM
from .exceptions import CapCutSignError


def compact_json(obj: Any) -> str:
    """Format python object into compact JSON string (no whitespace between separators)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def make_x_ss_stub(body_text: str) -> str:
    """Generate x-ss-stub header value (MD5 of request body)."""
    return hashlib.md5(body_text.encode("utf-8")).hexdigest()


def make_trace_id() -> str:
    """Generate unique W3C trace parent header ID."""
    seed = uuid.uuid4().hex[:32]
    return f"00-{seed}-{seed[:16]}-01"


def escape_xml(text: str) -> str:
    """Escape special XML characters for SSML generation."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _der_len(data: bytes, pos: int) -> tuple[int, int]:
    first = data[pos]
    pos += 1
    if first < 0x80:
        return first, pos
    nbytes = first & 0x7F
    return int.from_bytes(data[pos : pos + nbytes], "big"), pos + nbytes


def _der_value(data: bytes, pos: int, tag: int) -> tuple[bytes, int]:
    if data[pos] != tag:
        raise CapCutSignError(f"Bad DER tag: expected 0x{tag:02x}, got 0x{data[pos]:02x}")
    length, pos = _der_len(data, pos + 1)
    return data[pos : pos + length], pos + length


def _der_int(data: bytes, pos: int) -> tuple[int, int]:
    raw, pos = _der_value(data, pos, 0x02)
    return int.from_bytes(raw.lstrip(b"\x00"), "big"), pos


def rsa_public_numbers_from_pem(pem: str) -> tuple[int, int]:
    """Parse RSA modulus and exponent numbers from PEM formatted public key."""
    try:
        b64 = "".join(line for line in pem.splitlines() if not line.startswith("-----"))
        der = base64.b64decode(b64)
        outer, pos = _der_value(der, 0, 0x30)
        if pos != len(der):
            raise CapCutSignError("Trailing data in public key")
        _, pos = _der_value(outer, 0, 0x30)  # AlgorithmIdentifier
        bit_string, pos = _der_value(outer, pos, 0x03)
        if pos != len(outer) or not bit_string or bit_string[0] != 0:
            raise CapCutSignError("Bad subjectPublicKeyInfo")
        rsa_seq, pos = _der_value(bit_string[1:], 0, 0x30)
        if pos != len(bit_string[1:]):
            raise CapCutSignError("Trailing data in RSA public key")
        modulus, pos = _der_int(rsa_seq, 0)
        exponent, pos = _der_int(rsa_seq, pos)
        if pos != len(rsa_seq):
            raise CapCutSignError("Trailing integer data in RSA public key")
        return modulus, exponent
    except Exception as exc:
        if isinstance(exc, CapCutSignError):
            raise
        raise CapCutSignError(f"Failed to parse RSA PEM public key: {exc}") from exc


def rsa_encrypt_pkcs1v15(message: str | bytes, pem: str = TTS_SIGN_PUBLIC_KEY_PEM) -> str:
    """Encrypt message using RSA PKCS#1 v1.5 with pure Python standard library arithmetic.

    Returns Base64 encoded ciphertext.
    """
    modulus, exponent = rsa_public_numbers_from_pem(pem)
    key_len = (modulus.bit_length() + 7) // 8
    msg = message.encode("utf-8") if isinstance(message, str) else bytes(message)
    if len(msg) > key_len - 11:
        raise CapCutSignError("Message too long for RSA PKCS#1 v1.5 padding")
    ps_len = key_len - len(msg) - 3
    ps = bytearray()
    while len(ps) < ps_len:
        chunk = secrets.token_bytes(ps_len - len(ps))
        ps.extend(b for b in chunk if b != 0)
    encoded = b"\x00\x02" + bytes(ps[:ps_len]) + b"\x00" + msg
    encrypted = pow(int.from_bytes(encoded, "big"), exponent, modulus).to_bytes(key_len, "big")
    return base64.b64encode(encrypted).decode("ascii")


def make_tts_payload_sign(ssml: str, extra_info: str | None, device_id: str, app_id: str) -> str:
    """Generate RSA PKCS#1 v1.5 signature for TTS task inner payload."""
    ssml_md5 = hashlib.md5(ssml.encode("utf-8")).hexdigest()
    sign_input = f"appid:{app_id}&did:{device_id}&creditDisable:false&ssml:{ssml_md5}"
    if extra_info is not None:
        sign_input += f"&extraInfo:{extra_info}"
    return rsa_encrypt_pkcs1v15(sign_input)


def make_sign_header(url: str, appvr: str, device_time: str, tdid: str) -> str:
    """Generate CapCut HTTP request sign header."""
    path = url.split("?", 1)[0]
    sign_str = f"9e2c|{path[-7:]}|3|{appvr}|{device_time}|{tdid}|11ac"
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest()


def common_query(
    device: dict[str, Any], babi_param: Any = None, include_region: bool = True
) -> dict[str, str]:
    """Build standard query parameter dict for CapCut API calls."""
    q: dict[str, str] = {
        "app_name": device["app_name"],
        "device_type": device["device_type"],
        "os_version": device["os_version"],
        "channel": device["channel"],
        "version_name": device["version_name"],
        "device_brand": device["device_brand"],
        "device_id": device["device_id"],
        "iid": device["iid"],
        "version_code": device["version_code"],
        "device_platform": device["device_platform"],
        "aid": device["aid"],
    }
    if include_region:
        q["region"] = device["region"]
    if babi_param is not None:
        q["babi_param"] = compact_json(babi_param)
    return q


def base_headers(device: dict[str, Any], body_text: str, appid: bool = False) -> dict[str, str]:
    """Build base HTTP headers required by CapCut API endpoints."""
    now = str(int(time.time()))
    headers = {
        "content-type": "application/json",
        "appvr": device["appvr"],
        "ch": device["channel"],
        "device-time": now,
        "lan": device["lan"],
        "loc": device["loc"],
        "pf": device["pf"],
        "sign-ver": "1",
        "tdid": device["tdid"],
        "x-ss-stub": make_x_ss_stub(body_text),
        "x-ss-dp": device["aid"],
        "x-khronos": now,
        "x-tt-trace-id": make_trace_id(),
        "user-agent": "Cronet/TTNetVersion:1d7cc3b1 2025-07-16 QuicVersion:52c2b40d 2025-04-03",
        "accept-encoding": "gzip, deflate",
        "store-country-code": device["loc"].lower(),
        "store-country-code-src": "did",
        "is-dispatch-us-ttp": "0",
        "is-app-region-us-ttp": "0",
    }
    if appid:
        headers["app-sdk-version"] = device["appvr"]
        headers["appid"] = device["aid"]
    return headers
