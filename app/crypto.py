import os
from pathlib import Path
from cryptography.fernet import Fernet

ENV_PATH = Path(".env")

def _load_or_create_key() -> bytes:
    for var in ("ENCRYPTION_KEY", "FERNET_KEY", "SECRET_KEY"):
        val = os.getenv(var)
        if val:
            return val.strip().strip('"').strip("'").encode()

    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            _, val = line.split("=", 1)
            val = val.strip().strip('"').strip("'")
            if len(val) == 44 and val.endswith("="):
                return val.encode()

    # Otomatis buat file .env baru jika belum ada (untuk komputer teman)
    new_key = Fernet.generate_key().decode()
    ENV_PATH.write_text(f"ENCRYPTION_KEY={new_key}\n", encoding="utf-8")
    return new_key.encode()

_fernet = Fernet(_load_or_create_key())

def encrypt_token(token: str) -> str:
    return _fernet.encrypt(token.encode("utf-8")).decode("utf-8")

def decrypt_token(encrypted_token: str) -> str:
    return _fernet.decrypt(encrypted_token.encode("utf-8")).decode("utf-8")