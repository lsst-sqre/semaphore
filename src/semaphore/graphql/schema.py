"""GraphQL API schema (Strawberry-powered dataclasses)."""

from typing import List

import strawberry

__all__ = ["graphql_schema"]


@strawberry.type
class Book:
    title: str
    author: str


def get_books() -> List[Book]:
    return [
        Book(
            title="The Great Gatsby",
            author="F. Scott Fitzgerald",
        ),
    ]


@strawberry.type
class Query:
    books: List[Book] = strawberry.field(resolver=get_books)


graphql_schema = strawberry.Schema(query=Query)
