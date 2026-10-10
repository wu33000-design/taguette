-- Run as the Cloud SQL administrator in the taguette database.
-- Create login roles with strong passwords separately in the Cloud SQL console.
-- Do not commit passwords or paste them into this file.
REVOKE cloudsqlsuperuser FROM taguette_app, taguette_owner, taguette_backup;
ALTER ROLE taguette_app NOCREATEDB NOCREATEROLE;
ALTER ROLE taguette_owner NOCREATEDB NOCREATEROLE;
ALTER ROLE taguette_backup NOCREATEDB NOCREATEROLE;

REVOKE ALL ON DATABASE taguette FROM PUBLIC;
GRANT CONNECT ON DATABASE taguette
    TO taguette_app, taguette_owner, taguette_backup;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO taguette_owner;
GRANT USAGE ON SCHEMA public TO taguette_app, taguette_backup;

-- Apply before initialization; also covers tables introduced by migrations.
ALTER DEFAULT PRIVILEGES FOR ROLE taguette_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO taguette_app;
ALTER DEFAULT PRIVILEGES FOR ROLE taguette_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO taguette_app;
ALTER DEFAULT PRIVILEGES FOR ROLE taguette_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO taguette_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE taguette_owner IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO taguette_backup;

-- Also support applying this file to an already initialized database.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
    TO taguette_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO taguette_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO taguette_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO taguette_backup;
