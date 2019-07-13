#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Sync the database to elasticsearch index for use by Searching tools."""
from __future__ import print_function, absolute_import
from json import dumps
from time import sleep
from threading import Thread
try:
    from Queue import Queue
except ImportError:  # pragma: no cover
    from queue import Queue
from math import ceil
from datetime import datetime
from elasticsearch import Elasticsearch, ElasticsearchException, helpers
from pacifica.metadata.rest.objectinfo import ObjectInfoAPI
from .config import get_config
from .celery import CeleryQueue, SYNC_OBJECTS
from .search_render import ELASTIC_INDEX, SearchRender

ELASTIC_CONNECT_ATTEMPTS = 40
ELASTIC_WAIT = 3


def es_client():
    """Get the elasticsearch client object."""
    es_kwargs = {}
    if get_config().getboolean('elasticsearch', 'sniff'):
        es_kwargs['sniff_on_start'] = True
        es_kwargs['sniff_on_connection_fail'] = True
        es_kwargs['sniff_timeout'] = get_config().getint('elasticsearch', 'timeout')
    es_kwargs['timeout'] = get_config().getint('elasticsearch', 'timeout')
    esclient = Elasticsearch(
        [get_config().get('elasticsearch', 'url')],
        **es_kwargs
    )
    mapping_params = {
        'properties': {
            'key': {'type': 'keyword'},
            'value': {'type': 'keyword'},
            'key_value_pairs': {
                'properties': {
                    'key': {'type': 'keyword'},
                    'value': {'type': 'keyword'}
                }
            },
            'release': {
                'type': 'keyword'
            },
            'updated_date': {
                'type': 'date'
            },
            'created_date': {
                'type': 'date'
            },
            'transaction_ids': {
                'type':     'text',
                'fielddata': True
            },
            'has_doi': {
                'type':      'text',
                'fielddata': True
            },
            'description': {
                'type': 'keyword'
            },
            'type': {
                'type': 'keyword'
            },
            'keyword': {
                'type': 'keyword'
            },
            'users': {
                'properties': {
                    'keyword': {
                        'type': 'keyword'
                    },
                    'submitter': {
                        'properties': {
                            'keyword': {
                                'type': 'keyword'
                            }
                        }
                    },
                    'authorized_releaser': {
                        'properties': {
                            'keyword': {
                                'type': 'keyword'
                            }
                        }
                    }
                }
            },
            'instruments': {
                'properties': {
                    'keyword': {
                        'type': 'keyword'
                    }
                }
            },
            'projects': {
                'properties': {
                    'keyword': {
                        'type': 'keyword'
                    },
                    'closed_date': {
                        'type': 'date'
                    },
                    'actual_start_date': {
                        'type': 'date'
                    },
                    'actual_end_date': {
                        'type': 'date'
                    }
                }
            },
            'institutions': {
                'properties': {
                    'keyword': {
                        'type': 'keyword'
                    },

                }
            },
            'science_themes': {
                'properties': {
                    'keyword': {
                        'type': 'keyword'
                    },

                }
            },
            'groups': {
                'properties': {
                    'keyword': {
                        'type': 'keyword'
                    },
                }
            },
            'files': {
                'properties': {
                    'keyword': {
                        'type': 'keyword'
                    },
                    'created_date': {
                        'type': 'date'
                    }
                }
            }
        }
    }
    # pylint: disable=unexpected-keyword-arg
    esclient.indices.create(index=ELASTIC_INDEX, ignore=400)
    esclient.indices.put_mapping(
        index=ELASTIC_INDEX,
        doc_type='doc',
        body=dumps(mapping_params)
    )
    # pylint: enable=unexpected-keyword-arg
    return esclient


def try_es_connect(attempts=0):
    """Recursively try to connect to elasticsearch."""
    try:
        cli = es_client()
        cli.info()
    except ElasticsearchException as ex:  # pragma: no cover pulled from metadata
        if attempts < ELASTIC_CONNECT_ATTEMPTS:
            sleep(ELASTIC_WAIT)
            attempts += 1
            try_es_connect(attempts)
        else:
            raise ex


def start_work(work_queue):
    """The main thread for the work."""
    cli = es_client()
    job = work_queue.get()
    while job:
        print('Starting {object} ({time_field}): {page} of {num_pages}'.format(**job))
        try_doing_work(cli, job)
        work_queue.task_done()
        print('Finished {object} ({time_field}): {page} of {num_pages}'.format(**job))
        job = work_queue.get()
    work_queue.task_done()


def try_doing_work(cli, job):
    """Try doing some work even if you fail."""
    tries_left = 5
    success = False
    while not success and tries_left:
        try:
            helpers.bulk(cli, yield_data(**job))
            success = True
        except ElasticsearchException:  # pragma: no cover
            tries_left -= 1
    return success


def yield_data(**kwargs):
    """yield objects from obj for bulk ingest."""
    obj = kwargs['object']
    time_field = kwargs['time_field']
    page = kwargs['page']
    items_per_page = kwargs['items_per_page']
    time_delta = kwargs['time_delta']
    obj_cls = ObjectInfoAPI.get_class_object_from_name(obj)
    # pylint: disable=protected-access
    query = (obj_cls.select()
             .where(getattr(obj_cls, time_field) > time_delta)
             .order_by(obj_cls._meta.primary_key)
             .paginate(page, items_per_page))
    # pylint: enable=protected-access
    return SearchRender.generate(obj, [qobj.to_hash() for qobj in query], kwargs['exclude'])


def create_worker_threads(threads, work_queue):
    """Create the worker threads and return the list."""
    work_threads = []
    for _i in range(threads):
        wthread = Thread(target=start_work, args=(work_queue,))
        wthread.daemon = True
        wthread.start()
        work_threads.append(wthread)
    return work_threads


def generate_work(items_per_page, work_queue, time_ago, exclude):
    """Generate the work from the db and send it to the work queue."""
    now = datetime.now()
    time_delta = (now - time_ago).replace(microsecond=0)
    for obj in SYNC_OBJECTS:
        for time_field in ['created', 'updated']:
            kwargs = {
                time_field: time_delta.isoformat(),
                '{}_operator'.format(time_field): 'gt'
            }
            resp = ObjectInfoAPI.GET(obj, None, **kwargs)
            total_count = resp['record_count']
            num_pages = int(ceil(float(total_count) / items_per_page))
            for page in range(1, num_pages + 1):
                work_queue.put({
                    'object': obj,
                    'time_field': time_field,
                    'page': page,
                    'items_per_page': items_per_page,
                    'time_delta': time_delta,
                    'num_pages': num_pages+1,
                    'exclude': list(exclude)
                })


def search_sync(args):
    """Main search sync subcommand."""
    try_es_connect()
    if args.celery:
        work_queue = CeleryQueue()
    else:
        work_queue = Queue(32)
        work_threads = create_worker_threads(args.threads, work_queue)
    generate_work(args.items_per_page, work_queue, args.time_ago, args.exclude)
    if args.celery:
        return work_queue.progress()
    for _i in range(args.threads):
        work_queue.put(False)
    for wthread in work_threads:
        wthread.join()
    return work_queue.join()