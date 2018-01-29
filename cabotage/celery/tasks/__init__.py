from cabotage.server import celery

from .build import run_build


@celery.task()
def is_this_thing_on():
    print('mic check!')
