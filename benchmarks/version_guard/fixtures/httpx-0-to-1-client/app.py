"""Deliberately broken baseline for httpx-0-to-1-client."""
import versioned_api

def main():
    getattr(versioned_api, 'ClientLegacy')()

if __name__ == "__main__":
    try:
        main()
    except AttributeError as exc:
        raise RuntimeError("WRONG_VERSION_API:httpx:0.27.0:ClientLegacy") from exc
