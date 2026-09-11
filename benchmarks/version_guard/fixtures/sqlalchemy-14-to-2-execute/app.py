"""Deliberately broken baseline for sqlalchemy-14-to-2-execute."""
import versioned_api

def main():
    getattr(versioned_api, 'execute_legacy')()

if __name__ == "__main__":
    try:
        main()
    except AttributeError as exc:
        raise RuntimeError("WRONG_VERSION_API:sqlalchemy:2.0.29:execute_legacy") from exc
