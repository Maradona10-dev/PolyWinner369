print("STARTCONTROLE 21-10-A", flush=True)
"""Start de bestaande paperbot na CLOB-authenticatie. Geen live orders."""

import logging
import os
import re
import time

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data


HOST = "https://clob.polymarket.com"

FIELDS = {
    "apiKey": "POLYMARKET_API_KEY",
    "secret": "POLYMARKET_API_SECRET",
    "passphrase": "POLYMARKET_API_PASSPHRASE",
}


class AuthError(Exception):
    pass


def credentials(response):
    if response.status_code != 200:
        return None

    try:
        data = response.json()

        if not isinstance(data, dict):
            return None

        result = {
            key: data.get(key)
            for key in FIELDS
        }

        if all(
            isinstance(value, str) and value.strip()
            for value in result.values()
        ):
            return {
                key: value.strip()
                for key, value in result.items()
            }

    except (ValueError, TypeError):
        pass

    return None


def headers_for(account):
    timestamp = str(int(time.time()))

    message = encode_typed_data(
        domain_data={
            "name": "ClobAuthDomain",
            "version": "1",
            "chainId": 137,
        },
        message_types={
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ]
        },
        message_data={
            "address": account.address,
            "timestamp": timestamp,
            "nonce": 0,
            "message": (
                "This message attests that I control "
                "the given wallet"
            ),
        },
    )

    signature = account.sign_message(message).signature

    return {
        "POLY_ADDRESS": account.address,
        "POLY_SIGNATURE": "0x" + bytes(signature).hex(),
        "POLY_TIMESTAMP": timestamp,
        "POLY_NONCE": "0",
    }


def load_auth():
    # De Railway-variable blijft bestaan voor een herstart.
    private_key = os.environ.pop(
        "POLYMARKET_PRIVATE_KEY", ""
    ).strip()

    existing = {
        key: os.getenv(env, "").strip()
        for key, env in FIELDS.items()
    }

    if not private_key:
        if all(existing.values()):
            return existing

        raise AuthError(
            "POLYMARKET_PRIVATE_KEY ontbreekt; "
            "API-gegevens zijn ook niet compleet."
        )

    if not re.fullmatch(
        r"(?:0x)?[0-9a-fA-F]{64}",
        private_key,
    ):
        raise AuthError(
            "Private key heeft een ongeldig formaat. "
            "Gebruik de volledige exportsleutel."
        )

    try:
        account = Account.from_key(private_key)

    except Exception:
        raise AuthError(
            "Private key is ongeldig."
        ) from None

    finally:
        private_key = None

    # Alleen authenticatie. Geen orders of geldtransacties.
    # De private key zelf wordt niet verzonden.
    with requests.Session() as session:
        for attempt in range(3):
            try:
                response = session.get(
                    HOST + "/auth/derive-api-key",
                    headers=headers_for(account),
                    timeout=(5, 15),
                    allow_redirects=False,
                )

                result = credentials(response)

                if result:
                    return result

                if response.status_code in (400, 404):
                    response = session.post(
                        HOST + "/auth/api-key",
                        headers=headers_for(account),
                        timeout=(5, 15),
                        allow_redirects=False,
                    )

                    result = credentials(response)

                    if result:
                        return result

                print(
                    "Authenticatie niet voltooid "
                    f"(HTTP {response.status_code}); "
                    f"poging {attempt + 1}/3.",
                    flush=True,
                )

            except requests.RequestException:
                print(
                    "Authenticatieverbinding mislukt; "
                    f"poging {attempt + 1}/3.",
                    flush=True,
                )

            if attempt < 2:
                time.sleep(2 ** (attempt + 1))

    raise AuthError(
        "CLOB-authenticatie mislukt. "
        "Paperbot niet gestart; controleer "
        "bovenstaande HTTP-status."
    )


def main():
    print(
        "BOT.PY gestart — authenticatie voorbereiden.",
        flush=True,
    )

    for name in (
        "websockets",
        "urllib3",
        "requests",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)

    try:
        auth = load_auth()

    except AuthError as exc:
        print(
            f"START GESTOPT: {exc}",
            flush=True,
        )
        raise SystemExit(1) from None

    except Exception:
        print(
            "START GESTOPT: onverwachte authenticatiefout; "
            "details verborgen.",
            flush=True,
        )
        raise SystemExit(1) from None

    for key, env in FIELDS.items():
        os.environ[env] = auth[key]

    print(
        "CLOB API-gegevens beschikbaar. "
        "Start BTC Lead PAPER; geen live orders.",
        flush=True,
    )

    import strategy

    # Voorkomt opnieuw starten als per ongeluk de startcode
    # ook in strategy.py is geplakt.
    if not all(
        hasattr(strategy, name)
        for name in ("Bot", "Reference", "Store")
    ):
        raise SystemExit(
            "Verkeerde strategy.py: plak daar de volledige "
            "paperstrategie, niet de startcode."
        )

    strategy.main()


if __name__ == "__main__":
    main()
