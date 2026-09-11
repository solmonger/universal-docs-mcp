"""Synthetic fixture: a collection is built with the legacy form."""
from sqlalchemy import column, table

foo = table("foo", column("id"))


def main():
    return [foo.c.id]


if __name__ == "__main__":
    main()
