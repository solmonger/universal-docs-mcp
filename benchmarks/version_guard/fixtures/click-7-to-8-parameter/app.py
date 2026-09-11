"""Deliberately broken baseline for click-7-to-8-parameter."""
import versioned_api


def main():
    getattr(versioned_api, 'option')()

if __name__ == "__main__":
    try:
        main()
    except AttributeError as exc:
        raise RuntimeError("WRONG_VERSION_API:click:8.1.7:option") from exc
