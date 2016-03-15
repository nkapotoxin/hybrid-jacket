import webob
from hyperservice import exception
from hyperservice import wsgi
from hyperservice import utils
from hyperservice.common import log
from oslo.utils import importutils

import base64

LOG = log.getLogger(__name__)

import os
from oslo.config import cfg
CONF = cfg.CONF


class HostController(wsgi.Application):
        
    def list_volume(self, request):
        return {}

    def attach_volume(self, request, body):
        return webob.Response(status_int=204)

    def personality(self, request, dst_path, file_data):
        dst_dir = os.path.dirname(dst_path)
        if not os.path.isdir(dst_dir):
            os.makedirs(dst_dir)
        LOG.info("get personality with dst path:%s", dst_path)
        injected = False
        with open(dst_path, "wb") as dst:
            dst.write(base64.b64decode(file_data))
            injected = True
        if not injected:
            raise exception.InjectFailed(path=dst_path)
        return webob.Response(status_int=204)


def create_router(mapper):
    controller = HostController()
    mapper.connect('/host/volume',
                   controller=controller,
                   action='list_volume',
                   conditions=dict(method=['GET']))

    mapper.connect('/host/volume/action',
                   controller=controller,
                   action='attach_volume',
                   conditions=dict(method=['POST']))

    mapper.connect('/service/personality',
                   controller=controller,
                   action='personality',
                   conditions=dict(method=['POST']))