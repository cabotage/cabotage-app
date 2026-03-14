"""backfill commit_sha and ingress data onto releases and deployments

Revision ID: e5f9a2c7d834
Revises: c3d8e1f2a4b5
Create Date: 2026-03-14 12:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e5f9a2c7d834"
down_revision = "c3d8e1f2a4b5"
branch_labels = None
depends_on = None


# Reusable CTE that builds {name: {ingress asdict}} per application_environment_id
_INGRESS_DATA_CTE = """
    ingress_data AS (
        SELECT
            i.application_environment_id,
            jsonb_object_agg(
                i.name,
                jsonb_build_object(
                    'id', i.id::text,
                    'name', i.name,
                    'enabled', i.enabled,
                    'ingress_class_name', i.ingress_class_name,
                    'backend_protocol', i.backend_protocol,
                    'proxy_connect_timeout', i.proxy_connect_timeout,
                    'proxy_read_timeout', i.proxy_read_timeout,
                    'proxy_send_timeout', i.proxy_send_timeout,
                    'proxy_body_size', i.proxy_body_size,
                    'client_body_buffer_size', i.client_body_buffer_size,
                    'proxy_request_buffering', i.proxy_request_buffering,
                    'session_affinity', i.session_affinity,
                    'use_regex', i.use_regex,
                    'allow_annotations', i.allow_annotations,
                    'extra_annotations', COALESCE(i.extra_annotations, '{}'::jsonb),
                    'cluster_issuer', i.cluster_issuer,
                    'force_ssl_redirect', i.force_ssl_redirect,
                    'service_upstream', i.service_upstream,
                    'hosts', COALESCE(h_agg.hosts, '[]'::jsonb),
                    'paths', COALESCE(p_agg.paths, '[]'::jsonb)
                )
            ) AS data
        FROM ingresses i
        LEFT JOIN LATERAL (
            SELECT jsonb_agg(
                jsonb_build_object(
                    'id', ih.id::text,
                    'hostname', ih.hostname,
                    'tls_enabled', ih.tls_enabled,
                    'is_auto_generated', ih.is_auto_generated
                ) ORDER BY ih.hostname COLLATE "C"
            ) AS hosts
            FROM ingress_hosts ih
            WHERE ih.ingress_id = i.id
        ) h_agg ON true
        LEFT JOIN LATERAL (
            SELECT jsonb_agg(
                jsonb_build_object(
                    'id', ip.id::text,
                    'path', ip.path,
                    'path_type', ip.path_type,
                    'target_process_name', ip.target_process_name
                ) ORDER BY ip.path COLLATE "C"
            ) AS paths
            FROM ingress_paths ip
            WHERE ip.ingress_id = i.id
        ) p_agg ON true
        GROUP BY i.application_environment_id
    )
"""


def upgrade():
    # 1. Backfill commit_sha onto release image JSONB
    op.execute(
        """
        UPDATE project_app_releases r
        SET image = r.image || jsonb_build_object(
            'commit_sha',
            COALESCE(i.image_metadata->>'sha', 'null')
        )
        FROM project_app_images i
        WHERE i.id = (r.image->>'id')::uuid
          AND r.image->>'commit_sha' IS NULL
    """
    )

    # 2. Backfill ingresses on releases
    op.execute(
        f"""
        WITH {_INGRESS_DATA_CTE}
        UPDATE project_app_releases r
        SET ingresses = id.data
        FROM ingress_data id
        WHERE id.application_environment_id = r.application_environment_id
          AND r.ingresses = '{{}}'::jsonb
    """  # nosec B608 - _INGRESS_DATA_CTE is a hardcoded constant, not user input
    )

    # 3. Backfill ingresses onto the latest completed deployment per app_env.
    #    Only backfill the one we actually diff against; older deployments
    #    keep their original (empty) data since we can't reconstruct what
    #    was live at the time.
    op.execute(
        f"""
        WITH {_INGRESS_DATA_CTE},
        latest_completed AS (
            SELECT DISTINCT ON (application_environment_id) id
            FROM deployments
            WHERE complete = true
            ORDER BY application_environment_id, created DESC
        )
        UPDATE deployments d
        SET release = d.release || jsonb_build_object('ingresses', id.data)
        FROM ingress_data id, latest_completed lc
        WHERE d.id = lc.id
          AND id.application_environment_id = d.application_environment_id
          AND (d.release->'ingresses' IS NULL
               OR d.release->'ingresses' = '{{}}'::jsonb)
    """  # nosec B608 - _INGRESS_DATA_CTE is a hardcoded constant, not user input
    )

    # 4. Backfill commit_sha onto the latest completed deployment per app_env
    op.execute(
        """
        WITH latest_completed AS (
            SELECT DISTINCT ON (application_environment_id) id
            FROM deployments
            WHERE complete = true
            ORDER BY application_environment_id, created DESC
        )
        UPDATE deployments d
        SET release = jsonb_set(
            d.release,
            '{image,commit_sha}',
            to_jsonb(COALESCE(i.image_metadata->>'sha', 'null'))
        )
        FROM project_app_images i, latest_completed lc
        WHERE d.id = lc.id
          AND i.id = (d.release->'image'->>'id')::uuid
          AND d.release->'image'->>'commit_sha' IS NULL
          AND d.release->'image' IS NOT NULL
    """
    )


def downgrade():
    # Undo commit_sha on releases
    op.execute(
        """
        UPDATE project_app_releases
        SET image = image - 'commit_sha'
        WHERE image ? 'commit_sha'
    """
    )

    # Undo ingresses on releases
    op.execute(
        """
        UPDATE project_app_releases
        SET ingresses = '{}'::jsonb
    """
    )

    # Undo ingresses on deployments
    op.execute(
        """
        UPDATE deployments
        SET release = release - 'ingresses'
    """
    )

    # Undo commit_sha on deployments
    op.execute(
        """
        UPDATE deployments
        SET release = jsonb_set(
            release,
            '{image}',
            (release->'image') - 'commit_sha'
        )
        WHERE release->'image'->>'commit_sha' IS NOT NULL
    """
    )
