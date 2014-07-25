# coding: utf-8

"""
Support for Elasticsearch (1.0.0 or newer).

Provides an `ElasticsearchTarget` and a `CopyToIndex` template task.

Modeled after `luigi.contrib.rdbms.CopyToTable`.
----

A minimal example (assuming elasticsearch is running on localhost:9200):

    class ExampleIndex(CopyToIndex):
        index = 'example'

        def docs(self):
            return [{'_id': 1, 'title': 'An example document.'}]

    if __name__ == '__main__':
        task = ExampleIndex()
        luigi.build([task], local_scheduler=True)

----

All options:

    class ExampleIndex(CopyToIndex):
        host = 'localhost'
        port = 9200
        index = 'example'
        doc_type = 'default'
        purge_existing_index = True
        marker_index_hist_size = 1

        def docs(self):
            return [{'_id': 1, 'title': 'An example document.'}]

    if __name__ == '__main__':
        task = ExampleIndex()
        luigi.build([task], local_scheduler=True)

----

`Host`, `port`, `index`, `doc_type` parameters are standard elasticsearch.

`purge_existing_index` will delete the index, whenever an update is required.
This is useful, when one deals with "dumps" that represent the whole data,
not just updates.

`marker_index_hist_size` sets the maximum number of entries in the 'marker'
index. Keep all updates by default (0). Use 1 to only remember the most recent
update to the index. This can be useful, if an index needs to recreated, even
though the corresponding indexing task has been run sometime in the past - but
a later indexing task might have altered the index in the meantime.

There are a two luigi `client.cfg` configuration options:

    [elasticsearch]

    marker-index = update_log
    marker-doc-type = entry

"""

# pylint: disable=F0401,E1101,C0103
import abc
import datetime
import hashlib
import json
import logging
import luigi

logger = logging.getLogger('luigi-interface')

try:
    from elasticsearch.helpers import bulk_index
    import elasticsearch
    if elasticsearch.__version__ < (1, 0, 0):
        logger.warning("This module works with elasticsearch 1.0.0 "
                       "or newer only.")
except ImportError:
    logger.warning("Loading esindex module without elasticsearch installed. "
                   "Will crash at runtime if esindex functionality is used.")


class ElasticsearchTarget(luigi.Target):
    """ Target for a resource in Elasticsearch. """

    marker_index = luigi.configuration.get_config().get('elasticsearch',
                                        'marker-index', 'update_log')
    marker_doc_type = luigi.configuration.get_config().get('elasticsearch',
                                        'marker-doc-type', 'entry')

    def __init__(self, host, port, index, doc_type, update_id,
                 marker_index_hist_size=0):
        """
        Args:
            host (str): Elasticsearch server host
            port (int): Elasticsearch server port
            index (str): Index name
            doc_type (str): Doctype name
            update_id (str): An identifier for this data set
            marker_index_hist_size (int): List of changes to the index to remember
        """
        self.host = host
        self.port = port
        self.index = index
        self.doc_type = doc_type
        self.update_id = update_id
        self.marker_index_hist_size = marker_index_hist_size

        self.es = elasticsearch.Elasticsearch([{'host': self.host,
                                                'port': self.port}])

    def marker_index_document_id(self):
        """ Generate an id for the indicator document. """
        params = '%s:%s:%s' % (self.index, self.doc_type, self.update_id)
        return hashlib.sha1(params).hexdigest()

    def touch(self):
        """ Mark this update as complete. The document id would be sufficent,
        but we index the parameters

            (update_id, target_index, target_doc_type, date)

        as well for documentation. """
        self.create_marker_index()
        self.es.index(index=self.marker_index, doc_type=self.marker_doc_type,
                      id=self.marker_index_document_id(), body={
                      'update_id': self.update_id, 'target_index': self.index,
                      'target_doc_type': self.doc_type,
                      'date': datetime.datetime.now()})
        self.es.indices.flush(index=self.marker_index)
        self.ensure_hist_size()

    def exists(self):
        """ Test, if this task has been run. """
        try:
            _ = self.es.get(index=self.marker_index,
                            doc_type=self.marker_doc_type,
                            id=self.marker_index_document_id())
            return True
        except elasticsearch.NotFoundError:
            logger.debug('Marker document not found.')
        except elasticsearch.ElasticsearchException as err:
            logger.warn(err)
        return False

    def create_marker_index(self):
        """ Create the index that will keep track of the tasks if necessary. """
        if not self.es.indices.exists(index=self.marker_index):
            self.es.indices.create(index=self.marker_index)

    def ensure_hist_size(self):
        """ Shrink the history of updates for a `index/doc_type` combination
        down to `self.marker_index_hist_size`. """
        if self.marker_index_hist_size == 0:
            return
        result = self.es.search(index=self.marker_index,
                                doc_type=self.marker_doc_type,
                                body={'query': {
                                    'term': {'target_index': self.index}}},
                                sort=('date:desc',))

        for i, hit in enumerate(result.get('hits').get('hits'), start=1):
            if i > self.marker_index_hist_size:
                marker_document_id = hit.get('_id')
                self.es.delete(id=marker_document_id, index=self.marker_index,
                               doc_type=self.marker_doc_type)
        self.es.indices.flush(index=self.marker_index)


class CopyToIndex(luigi.Task):
    """
    Template task for inserting a data set into Elasticsearch.

    Usage:

    1. Subclass and override the required `index` attribute.

    2. Implement a custom `docs` method, that returns an iterable over
    the documents. A document can be a JSON string, e.g. from
    a newline-delimited JSON (ldj) file (default implementation) or some
    dictionary.

    Optional attributes:

    * doc_type (default),
    * host (localhost),
    * port (9200),
    * settings ({'settings': {}})
    * mapping (None),
    * chunk_size (2000),
    * raise_on_error (True),
    * purge_existing_index (False),
    * marker_index_hist_size (0)

    If settings are defined, they are only applied at index creation time.
    """

    @property
    def host(self):
        """ ES hostname """
        return 'localhost'

    @property
    def port(self):
        """ ES port """
        return 9200

    @abc.abstractproperty
    def index(self):
        """ The target index. May exists or not. """
        return None

    @property
    def doc_type(self):
        """ The target doc_type. """
        return 'default'

    @property
    def mapping(self):
        """ Dictionary with custom mapping or `None`. """
        return None

    @property
    def settings(self):
        """ Settings to be used at index creation time. """
        return {'settings': {}}

    @property
    def chunk_size(self):
        """ Single API call for this number of docs. """
        return 2000

    @property
    def raise_on_error(self):
        """ Whether to fail fast. """
        return True

    @property
    def purge_existing_index(self):
        """ Whether to delete the `index` completely before any indexing. """
        return False

    @property
    def marker_index_hist_size(self):
        """ Number of event log entries in the marker index. 0: unlimited. """
        return 0

    @property
    def timeout(self):
        """ Timeout. """
        return 10

    def docs(self):
        """ Return the documents to be indexed. Beside the user defined
        fields, the document may contain an `_index`, `_type` and `_id`. """
        with self.input().open('r') as fobj:
            for line in fobj:
                yield line

# everything below will rarely have to be overridden

    def _docs(self):
        """ Since `self.docs` may yield documents that do not explicitly
        contain `_index` or `_type`, add those attributes here, if necessary.
        """
        first = iter(self.docs()).next()
        needs_parsing = False
        if isinstance(first, basestring):
            needs_parsing = True
        elif isinstance(first, dict):
            pass
        else:
            raise RuntimeError('Document must be either JSON strings or dict.')
        for doc in self.docs():
            if needs_parsing:
                doc = json.loads(doc)
            if not '_index' in doc:
                doc['_index'] = self.index
            if not '_type' in doc:
                doc['_type'] = self.doc_type
            yield doc

    def create_index(self):
        """ Override to provide code for creating the target index.

        By default it will be created without any special settings or mappings.
        """
        es = elasticsearch.Elasticsearch([{'host': self.host,
                                           'port': self.port}])
        if not es.indices.exists(index=self.index):
            es.indices.create(index=self.index, body=self.settings)

    def delete_index(self):
        """ Delete the index, if it exists. """
        es = elasticsearch.Elasticsearch([{'host': self.host,
                                           'port': self.port}])
        if es.indices.exists(index=self.index):
            es.indices.delete(index=self.index)

    def update_id(self):
        """ This id will be a unique identifier for this indexing task."""
        return self.task_id

    def output(self):
        """ Returns a ElasticsearchTarget representing the inserted dataset.

        Normally you don't override this.
        """
        return ElasticsearchTarget(
            host=self.host,
            port=self.port,
            index=self.index,
            doc_type=self.doc_type,
            update_id=self.update_id(),
            marker_index_hist_size=self.marker_index_hist_size
         )

    def run(self):
        """ Purge existing index, if requested (`purge_existing_index`).
        Create the index, if missing. Apply mappings, if given.
        Set refresh interval to -1 (disable) for performance reasons.
        Bulk index in batches of size `chunk_size` (2000).
        Set refresh interval to 1s. Refresh Elasticsearch.
        Create entry in marker index.
        """
        if self.purge_existing_index:
            self.delete_index()
        self.create_index()
        es = elasticsearch.Elasticsearch([{'host': self.host,
                                           'port': self.port}],
                                         timeout=self.timeout)
        if self.mapping:
            es.indices.put_mapping(index=self.index, doc_type=self.doc_type,
                                   body=self.mapping)
        es.indices.put_settings({"index": {"refresh_interval": "-1"}},
                                index=self.index)

        bulk_index(es, self._docs(), chunk_size=self.chunk_size,
                   raise_on_error=self.raise_on_error)

        es.indices.put_settings({"index": {"refresh_interval": "1s"}},
                                index=self.index)
        es.indices.refresh()
        self.output().touch()