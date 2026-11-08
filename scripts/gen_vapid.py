"""Generate VAPID keys for Web Push and print .env lines.

Private key: base64url raw 32-byte EC key (accepted by pywebpush).
Public key: base64url uncompressed P-256 point (used as applicationServerKey in the browser).
"""
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def main():
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    print(f"VAPID_PUBLIC_KEY={b64(pub)}")
    print(f"VAPID_PRIVATE_KEY={b64(priv)}")


if __name__ == "__main__":
    main()
