"""Create a PostgreSQL custom-format dump and upload only after success."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


def backup(env=None, *, client=None, run=subprocess.run):
    env = dict(os.environ if env is None else env)
    instance = env['INSTANCE_CONNECTION_NAME']
    bucket = env['TAGUETTE_BACKUP_BUCKET']
    env.update(
        PGHOST='/cloudsql/' + instance,
        PGUSER=env.get('TAGUETTE_DB_USER', 'taguette_backup'),
        PGDATABASE=env.get('TAGUETTE_DB_NAME', 'taguette'),
        PGPASSWORD=Path(env['TAGUETTE_DB_PASSWORD_FILE']).read_text().rstrip(
            '\r\n'),
        PGCONNECT_TIMEOUT='10',
    )
    if not env['PGPASSWORD']:
        raise ValueError("Empty database password")
    if client is None:
        from google.cloud import storage
        client = storage.Client()
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')
    name = 'postgres/' + timestamp + '-' + uuid.uuid4().hex + '.dump'
    with tempfile.TemporaryDirectory(prefix='taguette_backup_') as tmp:
        path = Path(tmp) / 'taguette.dump'
        run(['pg_dump', '--format=custom', '--no-owner', '--no-acl',
             '--file', str(path)], env=env, check=True)
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError("Database dump is empty")
        client.bucket(bucket).blob(name).upload_from_filename(
            str(path), if_generation_match=0, checksum='auto',
        )
    print('Backup completed: gs://' + bucket + '/' + name)
    return name


if __name__ == '__main__':
    backup()
