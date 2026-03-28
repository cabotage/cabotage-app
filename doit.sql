BEGIN;

-- Running upgrade 5db73750bd83 -> ad75e4f600d2

UPDATE deployments
            SET release = jsonb_set(
                release,
                '{configuration}',
                (
                    SELECT coalesce(jsonb_object_agg(
                        key,
                        jsonb_build_object('buildtime', false) || value
                    ), '{}'::jsonb)
                    FROM jsonb_each(release->'configuration')
                )
            )
            WHERE release->'configuration' IS NOT NULL
              AND release->'configuration' != '{}'::jsonb;

UPDATE project_app_releases
            SET configuration = (
                SELECT coalesce(jsonb_object_agg(
                    key,
                    jsonb_build_object('buildtime', false) || value
                ), '{}'::jsonb)
                FROM jsonb_each(configuration)
            )
            WHERE configuration IS NOT NULL
              AND configuration != '{}'::jsonb;

UPDATE alembic_version SET version_num='ad75e4f600d2' WHERE alembic_version.version_num = '5db73750bd83';

COMMIT;

