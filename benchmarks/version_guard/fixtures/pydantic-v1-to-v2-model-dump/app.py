"""Synthetic fixture: a model integration using the pre-target method."""
from pydantic import BaseModel


class User(BaseModel):
    name: str


def main():
    return User(name="Ada").dict()


if __name__ == "__main__":
    main()
