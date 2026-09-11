"""Deliberately broken baseline for sqlalchemy-14-to-2-select."""
import versioned_api

def main():
    getattr(versioned_api, 'query')()

if __name__ == "__main__":
    try:
        main()
    except AttributeError as exc:
        raise RuntimeError("WRONG_VERSION_API:sqlalchemy:2.0.29:query") from exc
