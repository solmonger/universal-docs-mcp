"""Synthetic fixture: connection execution still uses the old owner."""
from sqlalchemy import create_engine


def main():
    engine = create_engine("sqlite://")
    return engine.run("select 1")


if __name__ == "__main__":
    main()
