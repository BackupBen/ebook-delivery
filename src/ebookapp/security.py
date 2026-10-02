"""Kryptografische Hilfsfunktionen: Passwörter, Tokens, Hashes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets

# scrypt-Parameter nach OWASP-Empfehlung (N=2^16, r=8, p=2, ca. 64 MB Speicher).
SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 2
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 256 * 1024 * 1024

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256

CODE_BYTES = 32  # 256 Bit Zufall pro Käuferlink
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
API_KEY_RE = re.compile(r"^ebk_[0-9a-f]{12}_[A-Za-z0-9_-]{43}$")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def needs_rehash(stored: str) -> bool:
    return not stored.startswith(f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$")


# Wird geprüft, wenn es den Benutzernamen nicht gibt, damit die Antwortzeit nichts verrät.
DUMMY_HASH = hash_password(secrets.token_urlsafe(24))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    """Öffentliche, nicht geheime Kennung."""
    return f"{prefix}_{secrets.token_hex(8)}"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_link_code() -> str:
    return _b64(secrets.token_bytes(CODE_BYTES))


def new_api_key() -> tuple[str, str]:
    """Liefert (vollständiger Schlüssel, öffentliches Präfix)."""
    prefix = f"ebk_{secrets.token_hex(6)}"
    return f"{prefix}_{_b64(secrets.token_bytes(32))}", prefix


def wrap_code(secret_key: bytes, scope: str, idempotency_key: str, link_id: str, code: str) -> str:
    """Verschlüsselt einen Link-Code für die Wiederholung einer idempotenten Anfrage.

    Der Schlüsselstrom hängt vom Server-Secret UND vom Idempotency-Key des Clients ab.
    Der Idempotency-Key selbst wird nur gehasht gespeichert; aus der Datenbank allein
    lässt sich der Code daher nicht zurückgewinnen.
    """
    raw = _unb64(code)
    pad = _wrap_pad(secret_key, scope, idempotency_key, link_id, len(raw))
    return _b64(bytes(a ^ b for a, b in zip(raw, pad, strict=True)))


def unwrap_code(
    secret_key: bytes, scope: str, idempotency_key: str, link_id: str, wrapped: str
) -> str:
    raw = _unb64(wrapped)
    pad = _wrap_pad(secret_key, scope, idempotency_key, link_id, len(raw))
    return _b64(bytes(a ^ b for a, b in zip(raw, pad, strict=True)))


def _wrap_pad(secret_key: bytes, scope: str, idempotency_key: str, link_id: str, n: int) -> bytes:
    if n > 32:
        raise ValueError("Code zu lang")
    message = "\0".join(["idem-wrap-v1", scope, idempotency_key, link_id]).encode("utf-8")
    return hmac.new(secret_key, message, hashlib.sha256).digest()[:n]


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def normalize_ip(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Liest eine Adresse und führt IPv4-in-IPv6-Schreibweisen auf IPv4 zurück.

    Manche Proxys melden IPv4-Clients als ``::ffff:a.b.c.d``. Ohne Normalisierung fielen
    alle diese Clients beim Kürzen auf ein IPv6-Präfix in denselben Topf.
    """
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address in _NAT64:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def ip_bucket(ip: str, v4_prefix: int, v6_prefix: int) -> str:
    """Kürzt eine Adresse auf ein Netzpräfix."""
    address = normalize_ip(ip)
    if address is None:
        return "unknown"
    prefix = v4_prefix if address.version == 4 else v6_prefix
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def rate_key(ip: str) -> str:
    """Schlüssel für Rate Limits: einzelne IPv4-Adresse bzw. IPv6-/64-Netz.

    Ein IPv6-Anschluss verfügt über ein ganzes /64; einzelne Adressen daraus zu zählen,
    wäre wirkungslos.
    """
    return ip_bucket(ip, 32, 64)


def client_fingerprint(secret_key: bytes, ip: str) -> str:
    """Kurzlebiger, nicht umkehrbarer Fingerabdruck eines Anschlusses für die Downloadzählung.

    Die Adresse wird auf /24 (IPv4) bzw. /56 (IPv6) gekürzt, damit übliche Adresswechsel
    während eines Downloads (Mobilfunk, Privacy Extensions) nicht als neuer Client zählen.
    Der User-Agent fließt bewusst nicht ein: Apps reichen Downloads häufig an einen
    Download-Manager mit anderem User-Agent weiter, was sonst doppelt gezählt würde.
    """
    message = "\0".join(["client-v2", ip_bucket(ip, 24, 56)]).encode("utf-8")
    return hmac.new(secret_key, message, hashlib.sha256).hexdigest()[:32]


def sign(secret_key: bytes, purpose: str, value: str) -> str:
    return hmac.new(secret_key, f"{purpose}\0{value}".encode(), hashlib.sha256).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
