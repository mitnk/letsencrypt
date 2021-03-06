"""Manual plugin."""
import os
import logging
import pipes
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import zope.component
import zope.interface

from acme import challenges

from letsencrypt import errors
from letsencrypt import interfaces
from letsencrypt.plugins import common


logger = logging.getLogger(__name__)


@zope.interface.implementer(interfaces.IAuthenticator)
@zope.interface.provider(interfaces.IPluginFactory)
class Authenticator(common.Plugin):
    """Manual Authenticator.

    This plugin requires user's manual intervention in setting up a HTTP
    server for solving http-01 challenges and thus does not need to be
    run as a privileged process. Alternatively shows instructions on how
    to use Python's built-in HTTP server.

    .. todo:: Support for `~.challenges.TLSSNI01`.

    """
    hidden = True

    description = "Manually configure an HTTP server"

    MESSAGE_TEMPLATE = """\
Make sure your web server displays the following content at
{uri} before continuing:

{validation}

If you don't have HTTP server configured, you can run the following
command on the target server (as root):

{command}
"""

    # a disclaimer about your current IP being transmitted to Let's Encrypt's servers.
    IP_DISCLAIMER = """\
NOTE: The IP of this machine will be publicly logged as having requested this certificate. \
If you're running letsencrypt in manual mode on a machine that is not your server, \
please ensure you're okay with that.

Are you OK with your IP being logged?
"""

    # "cd /tmp/letsencrypt" makes sure user doesn't serve /root,
    # separate "public_html" ensures that cert.pem/key.pem are not
    # served and makes it more obvious that Python command will serve
    # anything recursively under the cwd

    CMD_TEMPLATE = """\
mkdir -p {root}/public_html/{achall.URI_ROOT_PATH}
cd {root}/public_html
printf "%s" {validation} > {achall.URI_ROOT_PATH}/{encoded_token}
# run only once per server:
$(command -v python2 || command -v python2.7 || command -v python2.6) -c \\
"import BaseHTTPServer, SimpleHTTPServer; \\
s = BaseHTTPServer.HTTPServer(('', {port}), SimpleHTTPServer.SimpleHTTPRequestHandler); \\
s.serve_forever()" """
    """Command template."""

    def __init__(self, *args, **kwargs):
        super(Authenticator, self).__init__(*args, **kwargs)
        self._root = (tempfile.mkdtemp() if self.conf("test-mode")
                      else "/tmp/letsencrypt")
        self._httpd = None

    @classmethod
    def add_parser_arguments(cls, add):
        add("test-mode", action="store_true",
            help="Test mode. Executes the manual command in subprocess.")
        add("public-ip-logging-ok", action="store_true",
            help="Automatically allows public IP logging.")

    def prepare(self):  # pylint: disable=missing-docstring,no-self-use
        if self.config.noninteractive_mode and not self.conf("test-mode"):
            raise errors.PluginError("Running manual mode non-interactively is not supported")

    def more_info(self):  # pylint: disable=missing-docstring,no-self-use
        return ("This plugin requires user's manual intervention in setting "
                "up an HTTP server for solving http-01 challenges and thus "
                "does not need to be run as a privileged process. "
                "Alternatively shows instructions on how to use Python's "
                "built-in HTTP server.")

    def get_chall_pref(self, domain):
        # pylint: disable=missing-docstring,no-self-use,unused-argument
        return [challenges.HTTP01]

    def perform(self, achalls):  # pylint: disable=missing-docstring
        responses = []
        # TODO: group achalls by the same socket.gethostbyname(_ex)
        # and prompt only once per server (one "echo -n" per domain)
        for achall in achalls:
            responses.append(self._perform_single(achall))
        return responses

    @classmethod
    def _test_mode_busy_wait(cls, port):
        while True:
            time.sleep(1)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.connect(("localhost", port))
            except socket.error:  # pragma: no cover
                pass
            else:
                break
            finally:
                sock.close()

    def _perform_single(self, achall):
        # same path for each challenge response would be easier for
        # users, but will not work if multiple domains point at the
        # same server: default command doesn't support virtual hosts
        response, validation = achall.response_and_validation()

        # a hack for Nginx Ad-hoc use
        target_name, _ = validation.split('.')
        target_dir = os.path.expanduser('~/.well-known/acme-challenge/')
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)
        target_path = os.path.join(target_dir, target_name)
        with open(target_path, 'w') as f:
            f.write(validation)

        if not response.simple_verify(
                achall.chall, achall.domain,
                achall.account_key.public_key(), self.config.http01_port):
            logger.warning("Self-verify of challenge failed.")

        return response

    def cleanup(self, achalls):
        # pylint: disable=missing-docstring,no-self-use,unused-argument
        if self.conf("test-mode"):
            assert self._httpd is not None, (
                "cleanup() must be called after perform()")
            if self._httpd.poll() is None:
                logger.debug("Terminating manual command process")
                os.killpg(self._httpd.pid, signal.SIGTERM)
            else:
                logger.debug("Manual command process already terminated "
                             "with %s code", self._httpd.returncode)
            shutil.rmtree(self._root)
