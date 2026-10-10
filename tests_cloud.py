"""Cloud Run security, persistence and maintenance regression tests."""
import asyncio
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import close_all_sessions
from tornado.testing import gen_test

from taguette import cloud, database, main, web
from tests import DATABASE_URI, MyHTTPTestCase, set_dumb_password


spec = importlib.util.spec_from_file_location(
    'cloud_backup', Path(__file__).parent / 'contrib/cloud-run/backup.py')
backup_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_module)


class TestCloudConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        secret = Path(self.tmp.name) / 'cookie'
        secret.write_text('x' * 48)
        password = Path(self.tmp.name) / 'password'
        password.write_text('secret:password@with%characters\n')
        self.env = {
            'TAGUETTE_DOMAIN': 'taguette.example.org',
            'INSTANCE_CONNECTION_NAME': 'test-project:asia-east1:taguette-db',
            'TAGUETTE_SECRET_KEY_FILE': str(secret),
            'TAGUETTE_DB_PASSWORD_FILE': str(password),
            'PORT': '8081',
        }

    def test_cloud_config_requires_login_without_registration(self):
        config = cloud.build_config(self.env)
        self.assertTrue(config['MULTIUSER'])
        self.assertEqual(config['SINGLE_USER_LOGIN'], 'admin')
        self.assertFalse(config['REGISTRATION_ENABLED'])
        self.assertFalse(config['CREATE_TABLES'])
        self.assertTrue(config['REQUIRE_INITIALIZED_ADMIN'])
        self.assertIsNone(config['REDIS_SERVER'])
        self.assertEqual(config['PORT'], 8081)
        self.assertEqual(config['DEFAULT_LANGUAGE'], 'zh_TW')
        self.assertTrue(config['PREFER_DEFAULT_LANGUAGE'])
        self.assertEqual(cloud.build_config(dict(
            self.env, TAGUETTE_LANGUAGE='en_US'))['DEFAULT_LANGUAGE'], 'en_US')
        self.assertNotIn('secret:password', config['DATABASE']['url'])
        self.assertEqual(config['DATABASE']['connect_args']['password'],
                         'secret:password@with%characters')

    def test_config_rejects_missing_secrets_and_invalid_domain(self):
        env = dict(self.env)
        del env['TAGUETTE_DB_PASSWORD_FILE']
        with self.assertRaisesRegex(ValueError, 'Missing environment'):
            cloud.build_config(env)
        for domain in ['https://example.org', 'example.org/path', 'bad host']:
            with self.subTest(domain=domain):
                env = dict(self.env, TAGUETTE_DOMAIN=domain)
                with self.assertRaisesRegex(ValueError, 'hostname'):
                    cloud.build_config(env)

    def test_migration_preserves_connection_options_and_redacts_url(self):
        config = cloud.database_config(self.env)
        with mock.patch('taguette.database.engine_from_config') as factory:
            with mock.patch('alembic.command.upgrade') as upgrade:
                database.migrate(config, 'head')
        self.assertEqual(factory.call_args.args[0], config)
        alembic_config = upgrade.call_args.args[0]
        self.assertIs(alembic_config.attributes['engine'],
                      factory.return_value)
        self.assertNotIn('secret:password',
                         alembic_config.get_main_option('sqlalchemy.url'))
        factory.return_value.dispose.assert_called_once()

    def test_no_url_password_in_connection_log(self):
        config = {'url': 'postgresql://user:do-not-log@localhost/db'}
        with mock.patch('taguette.database.engine_from_config',
                        side_effect=RuntimeError('stop before connecting')):
            with self.assertLogs('taguette.database', level='INFO') as logs:
                with self.assertRaises(RuntimeError):
                    database.connect(config)
        self.assertNotIn('do-not-log', '\n'.join(logs.output))

    def test_backup_uploads_only_successful_dump(self):
        client = mock.Mock()
        env = dict(self.env, TAGUETTE_BACKUP_BUCKET='test-backups')

        def dump(args, **kwargs):
            self.assertNotIn('secret:password', ' '.join(args))
            self.assertEqual(kwargs['env']['PGPASSWORD'],
                             'secret:password@with%characters')
            Path(args[-1]).write_bytes(b'PGDMP-example')

        backup_module.backup(env, client=client, run=dump)
        client.bucket.return_value.blob.return_value.upload_from_filename\
            .assert_called_once()
        client.reset_mock()
        with self.assertRaises(subprocess.CalledProcessError):
            backup_module.backup(env, client=client, run=mock.Mock(
                side_effect=subprocess.CalledProcessError(1, ['pg_dump'])))
        client.bucket.assert_not_called()

    def test_backup_refuses_empty_dump(self):
        client = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, 'empty'):
            backup_module.backup(
                dict(self.env, TAGUETTE_BACKUP_BUCKET='test-backups'),
                client=client, run=mock.Mock())
        client.bucket.assert_not_called()


class TestCloudWeb(MyHTTPTestCase):
    def get_app(self):
        self.application = web.make_app(dict(
            main.DEFAULT_CONFIG, NAME='Private Taguette', PORT=8080,
            DATABASE=DATABASE_URI, DOMAIN='example.org', TOS_FILE=None,
            SECRET_KEY='x' * 48, MULTIUSER=True, SINGLE_USER_LOGIN='admin',
            REGISTRATION_ENABLED=False, FETCH_MESSAGES=False,
            PASSWORD_RESET_ENABLED=False, COOKIES_PROMPT=False,
            SECURE_COOKIES=True, EVENT_POLL_INTERVAL=30,
        ))
        self.io_loop.run_sync(
            lambda: set_dumb_password(self.application.DBSession))
        return self.application

    def tearDown(self):
        super().tearDown()
        close_all_sessions()
        engine = create_engine(DATABASE_URI)
        database.Base.metadata.drop_all(engine)
        engine.dispose()

    async def login(self):
        async with self.aget('/login') as response:
            self.assertEqual(response.status, 200)
        self.application.config['SECURE_COOKIES'] = False  # HTTP test server
        async with self.apost('/login',
                              data={'login': 'admin', 'password': 'hackme'})\
                as response:
            self.assertEqual(response.status, 303)

    @gen_test
    async def test_registration_reset_and_other_accounts_are_blocked(self):
        for path, status in [('/register', 403), ('/reset_password', 404),
                             ('/new_password', 404)]:
            async with self.aget(path) as response:
                self.assertEqual(response.status, status)
        async with self.aget('/login') as response:
            body = await response.read()
            self.assertNotIn(b'id="register-tab"', body)
            self.assertNotIn(b'Forgot password', body)
        with self.application.DBSession() as db:
            user = database.User(login='other')
            await user.set_password('hackme')
            db.add(user)
            db.commit()
        async with self.apost('/login',
                              data={'login': 'other', 'password': 'hackme'})\
                as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b'Invalid login or password', await response.read())

    @gen_test
    async def test_secure_login_cookie(self):
        async with self.aget('/login'):
            pass
        async with self.apost('/login',
                              data={'login': 'admin', 'password': 'hackme'})\
                as response:
            cookie = response.headers.get('Set-Cookie', '')
            self.assertIn('Secure', cookie)
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=Lax', cookie)

    @gen_test
    async def test_traditional_chinese_pages_and_javascript(self):
        self.application.config.update(
            DEFAULT_LANGUAGE='zh_TW', PREFER_DEFAULT_LANGUAGE=True)
        # The deployment default wins over an English browser preference.
        async with self.aget('/login', headers={
                'Accept-Language': 'en-US,en;q=0.9'}) as response:
            body = await response.text()
            self.assertIn('<html lang="zh-TW">', body)
            self.assertIn('登入', body)
            self.assertIn('密碼', body)
        async with self.aget('/trans.js') as response:
            self.assertEqual(response.status, 200)
            body = await response.text()
            self.assertIn('zh_TW', body)
            self.assertIn('\\u7de8\\u8f2f', body)  # 編輯 in JSON
        await self.login()
        async with self.aget('/') as response:
            self.assertIn('建立新專案', await response.text())
        with self.application.DBSession() as db:
            project = database.Project(name='中文研究', description='')
            project.members.append(database.ProjectMember(
                user_login='admin', privileges=database.Privileges.ADMIN))
            db.add(project)
            db.commit()
            project_id = project.id
        async with self.aget('/project/' + str(project_id)) as response:
            body = await response.text()
            for label in ['新增文件', '建立標籤', '合併標籤', '儲存並關閉']:
                self.assertIn(label, body)
            self.assertNotIn('>Merge tags<', body)
        async with self.aget('/missing-page') as response:
            self.assertEqual(response.status, 404)
            body = await response.text()
            self.assertIn('此頁面不存在', body)
            self.assertIn('按此返回首頁', body)
        async with self.aget('/account') as response:
            body = await response.text()
            self.assertIn('繁體中文', body)
            self.assertIn('預設語言', body)
        # An explicit account preference remains available.
        with self.application.DBSession() as db:
            db.query(database.User).get('admin').language = 'en_US'
            db.commit()
        async with self.aget('/account') as response:
            self.assertIn('<html lang="en-US">', await response.text())

    @gen_test
    async def test_short_events_and_collaboration_disabled(self):
        await self.login()
        with self.application.DBSession() as db:
            project = database.Project(name='Research', description='')
            project.members.append(database.ProjectMember(
                user_login='admin', privileges=database.Privileges.ADMIN))
            db.add(project)
            db.commit()
            project_id = project.id
        async with self.aget('/project/' + str(project_id)) as response:
            self.assertEqual(response.status, 200)
            body = await response.read()
            self.assertNotIn(b'javascript:showMembers()', body)
            self.assertIn(b'event_poll_interval = 30', body)
        from taguette import exact_version
        url = '/api/project/%d/events?version=%s&from=-1' % (
            project_id, exact_version())
        async with self.aget(url) as response:
            result = await asyncio.wait_for(response.json(), timeout=1)
            self.assertEqual(result, {'events': []})
        with self.application.DBSession() as db:
            cmd = database.Command.project_meta(
                'admin', project_id, 'Updated Research', '')
            db.add(cmd)
            db.commit()
        # Persisted events are delivered even with no in-process notification.
        async with self.aget(url) as response:
            result = await response.json()
            self.assertEqual(result['events'][0]['type'], 'project_meta')
        cookies = self.http_client.cookie_jar.filter_cookies(self.get_url('/'))
        async with self._fetch(
            '/api/project/%d/members' % project_id, method='PATCH',
            json={'admin': None},
            headers={'X-XSRFToken': cookies['_xsrf'].value},
        ) as response:
            self.assertEqual(response.status, 404)


@unittest.skipUnless(DATABASE_URI.startswith('postgresql'),
                     'Requires the PostgreSQL CI service')
class TestPostgresMaintenance(unittest.TestCase):
    def tearDown(self):
        close_all_sessions()
        engine = create_engine(DATABASE_URI)
        database.Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql('DROP TABLE IF EXISTS alembic_version')
        engine.dispose()

    def test_init_is_idempotent_and_reset_is_explicit(self):
        url = make_url(DATABASE_URI)
        config = {
            'url': url.set(password=None).render_as_string(),
            'connect_args': {'password': url.password},
        }
        asyncio.run(cloud.initialize(config, 'first-test-password'))
        asyncio.run(cloud.initialize(config, 'different-test-password'))
        Session = database.connect(config, create_tables=False)

        async def verify(password):
            with Session() as db:
                users = db.query(database.User).all()
                self.assertEqual(len(users), 1)
                return await users[0].check_password(password)

        self.assertTrue(asyncio.run(verify('first-test-password')))
        self.assertFalse(asyncio.run(verify('different-test-password')))
        asyncio.run(cloud.initialize(
            config, 'reset-test-password', reset=True))
        self.assertTrue(asyncio.run(verify('reset-test-password')))
        # Maintenance uses the same connect_args credentials as runtime.
        database.migrate(config, 'head')
        Session.kw['bind'].dispose()

    def test_runtime_refuses_empty_database(self):
        with self.assertRaises(database.NoSuchTableError):
            database.connect(DATABASE_URI, create_tables=False)

    def test_sql_roles_allow_data_writes_but_not_schema_changes(self):
        engine = create_engine(DATABASE_URI)
        roles = ['taguette_owner', 'taguette_app', 'taguette_backup']
        try:
            with engine.connect().execution_options(
                    isolation_level='AUTOCOMMIT') as conn:
                # Upstream tests leave Alembic's table outside Base.metadata.
                # Initialize a fresh schema owned by the maintenance role.
                conn.exec_driver_sql('DROP TABLE IF EXISTS alembic_version')
                conn.exec_driver_sql('CREATE ROLE cloudsqlsuperuser')
                for role in roles:
                    conn.exec_driver_sql(
                        "CREATE ROLE %s LOGIN PASSWORD 'test-role-password'"
                        % role)
                sql = (Path(__file__).parent /
                       'contrib/cloud-run/roles.sql').read_text()
                conn.exec_driver_sql(sql)

            def config(role):
                url = make_url(DATABASE_URI).set(username=role, password=None)
                return {'url': url.render_as_string(),
                        'connect_args': {'password': 'test-role-password'}}

            asyncio.run(cloud.initialize(
                config('taguette_owner'), 'test-admin-password'))
            app = database.connect(config('taguette_app'), create_tables=False)
            with app() as db:
                db.add(database.Project(name='中文研究', description=''))
                db.commit()
                self.assertEqual(db.query(database.Project).count(), 1)
            with app.kw['bind'].connect() as conn:
                with self.assertRaises(ProgrammingError):
                    conn.execute(text('CREATE TABLE forbidden (id integer)'))
            app.kw['bind'].dispose()
            reader = database.connect(config('taguette_backup'),
                                      create_tables=False)
            with reader() as db:
                self.assertEqual(db.query(database.Project).count(), 1)
            with reader.kw['bind'].connect() as conn:
                with self.assertRaises(ProgrammingError):
                    conn.execute(text('DELETE FROM projects'))
            reader.kw['bind'].dispose()
        finally:
            with engine.connect().execution_options(
                    isolation_level='AUTOCOMMIT') as conn:
                database.Base.metadata.drop_all(conn)
                for role in roles:
                    conn.exec_driver_sql('DROP OWNED BY ' + role)
                    conn.exec_driver_sql('DROP ROLE ' + role)
                conn.exec_driver_sql('DROP ROLE cloudsqlsuperuser')
            engine.dispose()


if __name__ == '__main__':
    unittest.main()
