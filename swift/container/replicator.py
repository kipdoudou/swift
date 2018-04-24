# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import itertools
import json
from collections import defaultdict
from eventlet import Timeout

from swift.container.sync_store import ContainerSyncStore
from swift.container.backend import ContainerBroker, DATADIR
from swift.container.reconciler import (
    MISPLACED_OBJECTS_ACCOUNT, incorrect_policy_index,
    get_reconciler_container_name, get_row_to_q_entry_translator)
from swift.common import db_replicator
from swift.common.storage_policy import POLICIES
from swift.common.swob import HTTPOk
from swift.common.exceptions import DeviceUnavailable
from swift.common.http import is_success
from swift.common.db import DatabaseAlreadyExists
from swift.common.utils import (Timestamp, hash_path,
                                storage_directory, majority_size,
                                make_db_file_path)


class ContainerReplicator(db_replicator.Replicator):
    server_type = 'container'
    brokerclass = ContainerBroker
    datadir = DATADIR
    default_port = 6201

    def __init__(self, conf, logger=None):
        super(ContainerReplicator, self).__init__(conf, logger=logger)
        self.reconciler_cleanups = self.sync_store = None

    def report_up_to_date(self, full_info):
        reported_key_map = {
            'reported_put_timestamp': 'put_timestamp',
            'reported_delete_timestamp': 'delete_timestamp',
            'reported_bytes_used': 'bytes_used',
            'reported_object_count': 'count',
        }
        for reported, value_key in reported_key_map.items():
            if full_info[reported] != full_info[value_key]:
                return False
        return True

    def _gather_sync_args(self, replication_info):
        parent = super(ContainerReplicator, self)
        sync_args = parent._gather_sync_args(replication_info)
        if len(POLICIES) > 1:
            sync_args += tuple(replication_info[k] for k in
                               ('status_changed_at', 'count',
                                'storage_policy_index'))
        return sync_args

    def _handle_sync_response(self, node, response, info, broker, http,
                              different_region=False):
        if is_success(response.status):
            remote_info = json.loads(response.data)
            if incorrect_policy_index(info, remote_info):
                status_changed_at = Timestamp.now()
                broker.set_storage_policy_index(
                    remote_info['storage_policy_index'],
                    timestamp=status_changed_at.internal)
            sync_timestamps = ('created_at', 'put_timestamp',
                               'delete_timestamp')
            if any(info[key] != remote_info[key] for key in sync_timestamps):
                broker.merge_timestamps(*(remote_info[key] for key in
                                          sync_timestamps))

            # Grab remote's shard ranges, too
            self._fetch_and_merge_shard_ranges(http, broker)

        return super(ContainerReplicator, self)._handle_sync_response(
            node, response, info, broker, http, different_region)

    def _sync_other_items(self, broker, http, local_id):
        # TODO: currently the number of shard ranges is expected to be _much_
        # less than normal objects so all are sync'd on each cycle. However, in
        # future there should be sync points maintained much like for object
        # syncing so that only new shard range rows are sync'd.
        other_items = broker.get_other_replication_items()
        if other_items:
            if not self._send_merge_items(http, local_id, other_items):
                return False
            self.logger.debug('%s synced %s other items to %s',
                              broker.db_file, len(other_items),
                              '%(ip)s:%(port)s/%(device)s' % http.node)
        return True

    def _choose_replication_mode(self, node, rinfo, info, local_sync, broker,
                                 http, different_region):
        # Always replicate shard ranges
        shard_range_success = self._sync_other_items(broker, http, info['id'])
        shard_ranges = broker.get_shard_ranges()
        # TODO: also check that own shard range state > ACTIVE?
        if shard_ranges:
            self.logger.warning(
                '%s is able to shard -- refusing to replicate objects to peer '
                '%s; currently have %d shard ranges and will wait for '
                'cleaving',
                broker.db_file,
                '%(ip)s:%(port)s/%(device)s' % node,
                len(shard_ranges))
            self.stats['deferred'] += 1
            return shard_range_success

        success = super(ContainerReplicator, self)._choose_replication_mode(
            node, rinfo, info, local_sync, broker, http,
            different_region)
        return shard_range_success and success

    def _fetch_and_merge_shard_ranges(self, http, broker):
        response = http.replicate('get_shard_ranges')
        if is_success(response.status):
            broker.merge_shard_ranges(json.loads(response.data))

    def _initialize_broker(self, device, part, account, container, epoch=None,
                           **kwargs):
        hsh = hash_path(account, container)
        db_dir = storage_directory(DATADIR, part, hsh)
        db_path = os.path.join(self.root, device, db_dir, hsh + '.db')
        if epoch:
            db_path = make_db_file_path(db_path, epoch)
        broker = ContainerBroker(db_path, account=account, container=container,
                                 logger=self.logger)
        if not os.path.exists(broker.db_file):
            try:
                broker.initialize(**kwargs)
            except DatabaseAlreadyExists:
                pass
        return broker

    def find_local_handoff_for_part(self, part):
        """
        Look through devices in the ring for the first handoff device that was
        identified during job creation as available on this node.

        :returns: a node entry from the ring
        """
        nodes = self.ring.get_part_nodes(part)
        more_nodes = self.ring.get_more_nodes(part)

        for node in itertools.chain(nodes, more_nodes):
            if node['id'] in self._local_device_ids:
                return node
        return None

    def get_reconciler_broker(self, timestamp):
        """
        Get a local instance of the reconciler container broker that is
        appropriate to enqueue the given timestamp.

        :param timestamp: the timestamp of the row to be enqueued

        :returns: a local reconciler broker
        """
        container = get_reconciler_container_name(timestamp)
        if self.reconciler_containers and \
                container in self.reconciler_containers:
            return self.reconciler_containers[container][1]
        account = MISPLACED_OBJECTS_ACCOUNT
        part = self.ring.get_part(account, container)
        node = self.find_local_handoff_for_part(part)
        if not node:
            raise DeviceUnavailable(
                'No mounted devices found suitable to Handoff reconciler '
                'container %s in partition %s' % (container, part))
        broker = self._initialize_broker(
            node['device'], part, account, container,
            put_timestamp=timestamp, storage_policy_index=0)
        if self.reconciler_containers is not None:
            self.reconciler_containers[container] = part, broker, node['id']
        return broker

    def feed_reconciler(self, container, item_list):
        """
        Add queue entries for rows in item_list to the local reconciler
        container database.

        :param container: the name of the reconciler container
        :param item_list: the list of rows to enqueue

        :returns: True if successfully enqueued
        """

        try:
            reconciler = self.get_reconciler_broker(container)
        except DeviceUnavailable as e:
            self.logger.warning('DeviceUnavailable: %s', e)
            return False
        self.logger.debug('Adding %d objects to the reconciler at %s',
                          len(item_list), reconciler.db_file)
        try:
            reconciler.merge_items(item_list)
        except (Exception, Timeout):
            self.logger.exception('UNHANDLED EXCEPTION: trying to merge '
                                  '%d items to reconciler container %s',
                                  len(item_list), reconciler.db_file)
            return False
        return True

    def dump_to_reconciler(self, broker, point):
        """
        Look for object rows for objects updates in the wrong storage policy
        in broker with a ``ROWID`` greater than the rowid given as point.

        :param broker: the container broker with misplaced objects
        :param point: the last verified ``reconciler_sync_point``

        :returns: the last successful enqueued rowid
        """
        max_sync = broker.get_max_row()
        misplaced = broker.get_misplaced_since(point, self.per_diff)
        if not misplaced:
            return max_sync
        translator = get_row_to_q_entry_translator(broker)
        errors = False
        low_sync = point
        while misplaced:
            batches = defaultdict(list)
            for item in misplaced:
                container = get_reconciler_container_name(item['created_at'])
                batches[container].append(translator(item))
            for container, item_list in batches.items():
                success = self.feed_reconciler(container, item_list)
                if not success:
                    errors = True
            point = misplaced[-1]['ROWID']
            if not errors:
                low_sync = point
            misplaced = broker.get_misplaced_since(point, self.per_diff)
        return low_sync

    def _post_replicate_hook(self, broker, info, responses):
        if info['account'] == MISPLACED_OBJECTS_ACCOUNT:
            return

        try:
            self.sync_store.update_sync_store(broker)
        except Exception:
            self.logger.exception('Failed to update sync_store %s' %
                                  broker.db_file)

        point = broker.get_reconciler_sync()
        if not broker.has_multiple_policies() and info['max_row'] != point:
            broker.update_reconciler_sync(info['max_row'])
            return
        max_sync = self.dump_to_reconciler(broker, point)
        success = responses.count(True) >= majority_size(len(responses))
        if max_sync > point and success:
            # to be safe, only slide up the sync point with a majority on
            # replication
            broker.update_reconciler_sync(max_sync)

    def cleanup_post_replicate(self, broker, orig_info, responses):
        debug_template = 'Not deleting db %s (%%s)' % broker.db_file
        if broker.requires_sharding():
            # despite being a handoff, since we're sharding we're not going to
            # do any cleanup so we can continue cleaving - this is still
            # considered "success"
            reason = 'requires sharding, state %s' % broker.get_db_state_text()
            self.logger.debug(debug_template, reason)
            return True
        return super(ContainerReplicator, self).cleanup_post_replicate(
            broker, orig_info, responses)

    def delete_db(self, broker):
        """
        Ensure that reconciler databases are only cleaned up at the end of the
        replication run.
        """
        if (self.reconciler_cleanups is not None and
                broker.account == MISPLACED_OBJECTS_ACCOUNT):
            # this container shouldn't be here, make sure it's cleaned up
            self.reconciler_cleanups[broker.container] = broker
            return
        if self.sync_store:
            try:
                # DB is going to get deleted. Be preemptive about it
                self.sync_store.remove_synced_container(broker)
            except Exception:
                self.logger.exception('Failed to remove sync_store entry %s' %
                                      broker.db_file)

        return super(ContainerReplicator, self).delete_db(broker)

    def replicate_reconcilers(self):
        """
        Ensure any items merged to reconciler containers during replication
        are pushed out to correct nodes and any reconciler containers that do
        not belong on this node are removed.
        """
        self.logger.info('Replicating %d reconciler containers',
                         len(self.reconciler_containers))
        for part, reconciler, node_id in self.reconciler_containers.values():
            self.cpool.spawn_n(
                self._replicate_object, part, reconciler.db_file, node_id)
        self.cpool.waitall()
        # wipe out the cache do disable bypass in delete_db
        cleanups = self.reconciler_cleanups
        self.reconciler_cleanups = self.reconciler_containers = None
        self.logger.info('Cleaning up %d reconciler containers',
                         len(cleanups))
        for reconciler in cleanups.values():
            self.cpool.spawn_n(self.delete_db, reconciler)
        self.cpool.waitall()
        self.logger.info('Finished reconciler replication')

    def run_once(self, *args, **kwargs):
        self.reconciler_containers = {}
        self.reconciler_cleanups = {}
        self.sync_store = ContainerSyncStore(self.root,
                                             self.logger,
                                             self.mount_check)
        rv = super(ContainerReplicator, self).run_once(*args, **kwargs)
        if any([self.reconciler_containers, self.reconciler_cleanups]):
            self.replicate_reconcilers()
        return rv

    def _in_sync(self, rinfo, info, broker, local_sync):
        # TODO: don't always sync shard ranges!
        if broker.get_shard_ranges(include_own=True, include_deleted=True):
            return False

        return super(ContainerReplicator, self)._in_sync(
            rinfo, info, broker, local_sync)

    def _is_locked(self, broker):
        return broker.has_sharding_lock()


class ContainerReplicatorRpc(db_replicator.ReplicatorRpc):

    def _db_file_exists(self, db_path):
        return bool(db_replicator.get_db_files(db_path))

    def _parse_sync_args(self, args):
        parent = super(ContainerReplicatorRpc, self)
        remote_info = parent._parse_sync_args(args)
        if len(args) > 9:
            remote_info['status_changed_at'] = args[7]
            remote_info['count'] = args[8]
            remote_info['storage_policy_index'] = args[9]
        return remote_info

    def _get_synced_replication_info(self, broker, remote_info):
        """
        Sync the remote_info storage_policy_index if needed and return the
        newly synced replication info.

        :param broker: the database broker
        :param remote_info: the remote replication info

        :returns: local broker replication info
        """
        info = broker.get_replication_info()
        if incorrect_policy_index(info, remote_info):
            status_changed_at = Timestamp.now().internal
            broker.set_storage_policy_index(
                remote_info['storage_policy_index'],
                timestamp=status_changed_at)
            info = broker.get_replication_info()
        return info

    def _abort_rsync_then_merge(self, db_file, old_filename):
        if super(ContainerReplicatorRpc, self)._abort_rsync_then_merge(
                db_file, old_filename):
            return True
        # if the local db has started sharding since the original 'sync'
        # request then abort object replication now; instantiate a fresh broker
        # each time this check if performed so to get latest state
        broker = ContainerBroker(db_file)
        return broker.requires_sharding() or broker.is_sharded()

    def get_shard_ranges(self, broker, args):
        shard_ranges = broker.get_shard_ranges(include_deleted=True,
                                               include_own=True)
        return HTTPOk(headers={'Content-Type': 'application/json'},
                      body=json.dumps([dict(sr) for sr in shard_ranges]))
