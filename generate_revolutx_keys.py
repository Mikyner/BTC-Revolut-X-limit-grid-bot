"""
Vygeneruje Ed25519 klíčový pár pro Revolut X API.

Spusť tento skript JEDNOU lokálně (nebo v kontejneru přes `docker exec`),
než zapneš live obchodování. Privátní klíč se uloží do data/revolutx_private.pem
a NIKDY by neměl opustit server. Veřejný klíč se vypíše na obrazovku -
ten zkopíruješ do Revolut X appky (Profile > API Keys > Add public key).

Použití:
    python generate_revolutx_keys.py
"""

from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import config

def main():
    output_path = Path(config.REVOLUTX_PRIVATE_KEY_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        confirm = input(
            f"Soubor {output_path} už existuje. Přepsat a vygenerovat NOVÝ klíč? "
            f"(stará klíč přestane fungovat pro nové requesty) [y/N]: "
        )
        if confirm.lower() != "y":
            print("Zrušeno, ponechávám existující klíč.")
            return

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    output_path.write_bytes(private_pem)
    output_path.chmod(0o600)

    print(f"Privátní klíč uložen do: {output_path} (oprávnění 600 - jen ty máš čtení)")
    print()
    print("=" * 60)
    print("ZKOPÍRUJ TENTO VEŘEJNÝ KLÍČ DO REVOLUT X APP:")
    print("(Profile > API Keys > Add public key)")
    print("=" * 60)
    print()
    print(public_pem.decode())
    print("=" * 60)
    print()
    print("Po zaregistrování veřejného klíče ti Revolut X vygeneruje API KEY")
    print("(64znakový alfanumerický řetězec) - ten nastav jako REVOLUTX_API_KEY")
    print("v souboru .env.")


if __name__ == "__main__":
    main()
