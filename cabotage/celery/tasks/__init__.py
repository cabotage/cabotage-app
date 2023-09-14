from celery import shared_task

from .build import (
    run_image_build,  # noqa: F401
    run_release_build,  # noqa: F401
)

from .deploy import (
    run_deploy,  # noqa: F401
)

from .github import process_github_hook  # noqa: F401


@shared_task()
def is_this_thing_on():
    print("mic check!")
