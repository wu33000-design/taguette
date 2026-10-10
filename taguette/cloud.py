"""Cloud Run configuration and explicit, non-interactive maintenance jobs.

Credentials are read from Secret Manager file mounts, never CLI arguments.
The web process does not create tables or bootstrap passwords.
"""
import argparse
import asyncio
import logging
import os
from pathlib import Path
import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from . import database, main as taguette_main, validate


logger = logging.getLogger(__name__)


def required(env, name):
    value = env.get(name, '')
    if not value:
        raise ValueError("Missing environment variable: " + name)
    return value


def read_secret(env, name):
    path = required(env, name)
    value = Path(path).read_text().rstrip('\r\n')
    if not value:
        raise ValueError("Empty secret file: " + name)
    return value


def database_config(env=None):
    env = os.environ if env is None else env
    instance = env.get('INSTANCE_CONNECTION_NAME', '')
    host = env.get('TAGUETTE_DB_HOST', '')
    if not host:
        if not re.fullmatch(r'[a-z0-9-]+:[a-z0-9-]+:[a-z0-9-]+', instance):
            raise ValueError("Set a valid INSTANCE_CONNECTION_NAME")
        host = '/cloudsql/' + instance
    url = URL.create(
        'postgresql+psycopg2',
        username=env.get('TAGUETTE_DB_USER', 'taguette_app'),
        database=env.get('TAGUETTE_DB_NAME', 'taguette'),
        query={'host': host},
    )
    return {
        'url': url.render_as_string(hide_password=True),
        'connect_args': {
            'password': read_secret(env, 'TAGUETTE_DB_PASSWORD_FILE'),
            'connect_timeout': 10,
        },
        'pool_size': 2,
        'max_overflow': 1,
        'pool_timeout': 30,
        'pool_pre_ping': True,
        'hide_parameters': True,
    }


def build_config(env=None):
    env = os.environ if env is None else env
    domain = required(env, 'TAGUETTE_DOMAIN')
    if not re.fullmatch(r'[A-Za-z0-9.-]+(?::[0-9]+)?', domain):
        raise ValueError("TAGUETTE_DOMAIN must be a hostname, "
                         "without https://")
    secret_key = read_secret(env, 'TAGUETTE_SECRET_KEY_FILE')
    if len(secret_key) < 32:
        raise ValueError("Cookie signing secret needs at least 32 characters")
    config = dict(taguette_main.DEFAULT_CONFIG)
    config.update(
        NAME=env.get('TAGUETTE_NAME', 'Taguette'),
        PORT=int(env.get('PORT', '8080')),
        BIND_ADDRESS='0.0.0.0', BASE_PATH='', DOMAIN=domain,
        SECRET_KEY=secret_key, DATABASE=database_config(env),
        # Keep password authentication while limiting access to one account.
        MULTIUSER=True, SINGLE_USER_LOGIN='admin',
        REGISTRATION_ENABLED=False, REDIS_SERVER=None,
        CREATE_TABLES=False, REQUIRE_INITIALIZED_ADMIN=True,
        SECURE_COOKIES=True, PASSWORD_RESET_ENABLED=False,
        FETCH_MESSAGES=False, EVENT_POLL_INTERVAL=30,
        X_HEADERS=True, TOS_FILE=None, COOKIES_PROMPT=False,
        EMAIL='Taguette <noreply@' + domain.split(':')[0] + '>',
        MAIL_SERVER={'host': 'localhost', 'port': 25},
        DEFAULT_LANGUAGE=env.get('TAGUETTE_LANGUAGE', 'zh_TW'),
        PREFER_DEFAULT_LANGUAGE=True,
        CONVERT_TO_HTML_TIMEOUT=180, CONVERT_FROM_HTML_TIMEOUT=180,
    )
    if not 1 <= config['PORT'] <= 65535:
        raise ValueError("Invalid PORT")
    return config


async def initialize(db_config, password, *, reset=False):
    validate.user_password(password)
    if len(password) < 12:
        raise ValueError("Admin password needs at least 12 characters")
    # Serialize initialization/reset jobs, including repeated executions.
    engine = create_engine(
        db_config['url'], connect_args=db_config['connect_args'],
        hide_parameters=True,
    )
    try:
        with engine.connect() as lock:
            lock.execute(text('SELECT pg_advisory_lock(7465)'))
            try:
                DBSession = database.connect(db_config,
                                             create_tables=not reset)
                with DBSession() as db:
                    admin = db.query(database.User).get('admin')
                    if admin is None:
                        if reset:
                            raise ValueError("Run init before reset-password")
                        admin = database.User(login='admin')
                        db.add(admin)
                    elif admin.hashed_password and not reset:
                        logger.info("Admin already initialized; unchanged")
                        return
                    await admin.set_password(password)
                    admin.disabled = False
                    db.commit()
                logger.info("Admin account initialized" if not reset
                            else "Admin password reset")
            finally:
                lock.execute(text('SELECT pg_advisory_unlock(7465)'))
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'migrate',
                                            'reset-password'])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    db_config = database_config()
    if args.command == 'migrate':
        database.migrate(db_config, 'head')
    else:
        password = read_secret(os.environ, 'TAGUETTE_ADMIN_PASSWORD_FILE')
        asyncio.run(initialize(db_config, password,
                               reset=args.command == 'reset-password'))


if __name__ == '__main__':
    main()
