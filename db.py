"""Shared PostgreSQL connection factory.

All modules in this project should import ``get_connection`` from here rather
than building their own ``psycopg2.connect`` call, so credentials and SSL
settings are configured in exactly one place.

Environment variables (loaded from ``.env``):
    DB_HOST: Database host name or IP address.
    DB_PORT: Database port (default: 5432).
    DB_NAME: Database name.
    DB_USER: Database user.
    DB_PASSWORD: Database password.
    DB_SSLMODE: SSL mode (default: require).
"""

import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()

_DB_CONFIG: dict = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT", "5432"),
    "database": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "sslmode": os.getenv("DB_SSLMODE", "require"),
}


def get_connection() -> psycopg2.extensions.connection:
    """Open and return a new psycopg2 database connection.

    The connection is configured from environment variables so callers do not
    need to know the credentials.  The caller is responsible for closing the
    connection (or using it as a context manager).

    Returns:
        psycopg2.extensions.connection: An open, ready-to-use connection.

    Raises:
        psycopg2.OperationalError: If the database is unreachable or
            credentials are invalid.
    """
    return psycopg2.connect(**_DB_CONFIG)
