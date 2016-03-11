import webob

from hyperservice import exception
from hyperservice import wsgi

from hyperservice.common import log
from hyperservice.common import importutils
from hyperservice import utils
from hyperservice.i18n import _
from hyperservice.docker_client import DockerHTTPClient
from hyperservice.libvirt import network

import functools
import uuid
import inspect
import six
import os

from oslo.config import cfg

from docker import errors


CONF = cfg.CONF

LOG = log.getLogger(__name__)

class ContainerController(wsgi.Application):
        
    def __init__(self):
        self._docker = None
        self._container = None
        vif_class = importutils.import_class(CONF.docker.vif_driver)
        self.vif_driver = vif_class()
        super(ContainerController, self).__init__()

    @property
    def docker(self):
        if self._docker is None:
            self._docker = DockerHTTPClient(CONF.docker.host_url)
        return self._docker

    @property
    def container(self):
        if self._container is None:
            containers = self.docker.containers(all=True)
            # containers = self.docker.containers(quiet=True, all=True)
            if not containers:
                LOG.error("No containers exists!")
                raise exception.ContainerNotFound()
            if len(containers) > 1:
                LOG.warn("Have multiple(%d) containers: %s !", len(containers), containers)
            self._container = { "id" : containers[0]["id"], 
                    "name" : (containers[0]["names"] or ["ubuntu-upstart"]) [0]}
        return self._container

    def plug_vifs(self, network_info):
        """Plug VIFs into networks."""
        instance = self.container['id']
        for vif in network_info:
            LOG.debug("plug vif %s", vif)
            self.vif_driver.plug(instance, vif)

    def _find_container_pid(self, container_id):
        n = 0
        while True:
            # NOTE(samalba): We wait for the process to be spawned inside the
            # container in order to get the the "container pid". This is
            # usually really fast. To avoid race conditions on a slow
            # machine, we allow 10 seconds as a hard limit.
            if n > 20:
                return
            info = self.docker.inspect_container(container_id)
            if info:
                pid = info['State']['Pid']
                # Pid is equal to zero if it isn't assigned yet
                if pid:
                    return pid
            time.sleep(0.5)
            n += 1

    def _attach_vifs(self, network_info):
        """Plug VIFs into container."""
        if not network_info:
            return

        container_id = self.container['id']
        netns_path = '/var/run/netns'
        if not os.path.exists(netns_path):
            utils.execute(
                'mkdir', '-p', netns_path, run_as_root=True)
        nspid = self._find_container_pid(container_id)
        if not nspid:
            msg = _('Cannot find any PID under container "{0}"')
            raise RuntimeError(msg.format(container_id))
        netns_path = os.path.join(netns_path, container_id)
        utils.execute(
            'ln', '-sf', '/proc/{0}/ns/net'.format(nspid),
            '/var/run/netns/{0}'.format(container_id),
            run_as_root=True)
        utils.execute('ip', 'netns', 'exec', container_id, 'ip', 'link',
                      'set', 'lo', 'up', run_as_root=True)

        instance = container_id
        for vif in network_info:
            self.vif_driver.attach(vif, instance, container_id)

    def create(self, request, image_name, volume_id=None):
        """ create the container. """
        if volume_id:
            # Create VM from volume, create a symbolic link for the device.
            LOG.info("create new container from volume %s", volume_id)
            pass
        # return webob.Response(status="201 Created", body='{ "id" : "testid" } ')
        return self.docker.create_container(image_name, network_disabled=True)

    def start(self, request, network_info={}, block_device_info={}):
        """ Start the container. """
        container_id = self.container['id']
        LOG.info("start container %s network_info %s block_device_info ", 
                     container_id, network_info, block_device_info)
        self.docker.start(container_id)
        if not network_info:
            return
        try:
            self.plug_vifs(network_info)
            self._attach_vifs(network_info)
        except Exception as e:
            msg = _('Cannot setup network for container {0}: {1}').format(self.container['name'], e)
            LOG.debug(msg, exc_info=True)
            raise exception.ContainerStartFailed(msg)
                                                  
    def _stop(self, container_id, timeout=5):
        try:
            self.docker.stop(container_id, max(timeout, 5))
        except errors.APIError as e:
            if 'Unpause the container before stopping' not in e.explanation:
                LOG.warning(_('Cannot stop container: %s'),
                            e, instance=container_id, exc_info=True)
                raise
            self.docker.unpause(container_id)
            self.docker.stop(container_id, timeout)

    def stop(self, request):
        """ Stop the container. """
        container_id = self.container['id']
        LOG.info("stop container %s", container_id)
        return self._stop(container_id)

    def _extract_dns_entries(self, network_info):
        dns = []
        if network_info:
            for net in network_info:
                subnets = net['network'].get('subnets', [])
                for subnet in subnets:
                    dns_entries = subnet.get('dns', [])
                    for dns_entry in dns_entries:
                        if 'address' in dns_entry:
                            dns.append(dns_entry['address'])
        return dns if dns else None

    def unplug_vifs(self, network_info):
        """Unplug VIFs from networks."""
        instance = self.container['id']
        for vif in network_info:
            self.vif_driver.unplug(instance, vif)

    def restart(self, request, network_info={}, block_device_info={}):
        """ Restart the container. """
        # return webob.Response(status_int=204)
        container_id = self.container['id']
        LOG.info("restart container %s", container_id)
        self._stop(container_id)
        try:
            network.teardown_network(container_id)
            if network_info:
                # self.unplug_vifs(network_info)
                netns_file = '/var/run/netns/{0}'.format(container_id)
                # if os.path.exists(netns_file):
                    # os.remove(netns_file)
        except Exception as e:
            LOG.warning(_('Cannot destroy the container network'
                          ' during reboot {0}').format(e),
                        exc_info=True)
            return

        dns = self._extract_dns_entries(network_info)
        self.docker.start(container_id, dns=dns)
        try:
            if network_info:
                self.plug_vifs(network_info)
                self._attach_vifs(network_info)
        except Exception as e:
            LOG.warning(_('Cannot setup network on reboot: {0}'), e,
                        exc_info=True)
            return

def create_router(mapper):
    controller = ContainerController()
    mapper.connect('/container/create',
                   controller=controller,
                   action='create',
                   conditions=dict(method=['POST']))
    mapper.connect('/container/start',
                   controller=controller,
                   action='start',
                   conditions=dict(method=['POST']))
    mapper.connect('/container/stop',
                   controller=controller,
                   action='stop',
                   conditions=dict(method=['POST']))
    mapper.connect('/container/restart',
                   controller=controller,
                   action='restart',
                   conditions=dict(method=['POST']))
