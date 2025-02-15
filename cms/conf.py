#!/usr/bin/env python3

# Contest Management System - http://cms-dev.github.io/
# Copyright © 2010-2014 Giovanni Mascellani <mascellani@poisson.phc.unipi.it>
# Copyright © 2010-2018 Stefano Maggiolo <s.maggiolo@gmail.com>
# Copyright © 2010-2012 Matteo Boscariol <boscarim@hotmail.com>
# Copyright © 2013 Luca Wehrstedt <luca.wehrstedt@gmail.com>
# Copyright © 2014 Fabian Gundlach <320pointsguy@gmail.com>
# Copyright © 2016 Myungwoo Chun <mc.tamaki@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import errno
import ipaddress
import pyjson5
import logging
import os
import socket
import sys
from collections import namedtuple
from contextlib import closing
from typing import Dict

from .log import set_detailed_logs


logger = logging.getLogger(__name__)


class Address(namedtuple("Address", "ip port")):
    def __repr__(self):
        return "%s:%d" % (self.ip, self.port)


class ServiceCoord(namedtuple("ServiceCoord", "name shard")):
    """A compact representation for the name and the shard number of a
    service (thus identifying it).

    """
    def __repr__(self):
        return "%s,%d" % (self.name, self.shard)


class ConfigError(Exception):
    """Exception for critical configuration errors."""
    pass


class EphemeralServiceConfig:
    """Configuration of an ephemeral service. An ephemeral service is a
    normal service whose shard is chosen depending on its address and
    port. The port is assigned inside a range and the address must be
    inside the subnet.
    """
    EPHEMERAL_SHARD_OFFSET = 1000

    def __init__(self, subnet, min_port, max_port):
        self.subnet = ipaddress.ip_network(subnet)
        self.min_port = min_port
        self.max_port = max_port
        if min_port > max_port:
            raise ConfigError("Invalid port range: [%s, %s]" \
                % (min_port, max_port))

    def get_shard(self, address, port):
        """Get the ephemeral shard for a service given its address and port.

        address (IPv4Address|IPv6Address): address of the service.
        port (int): port of the service.

        return (int): shard of the service
        """
        if address not in self.subnet:
            raise ValueError("The address is not inside the subnet")
        host_id = int(address) & int(self.subnet.hostmask)
        num_ports = self.max_port - self.min_port + 1
        shard = host_id * num_ports + (port - self.min_port)
        return shard + self.EPHEMERAL_SHARD_OFFSET

    def get_address(self, shard):
        """Get the address and port of a service given its shard.

        shard (int): shard of the service

        return (Address): address and port of the service
        """
        shard -= self.EPHEMERAL_SHARD_OFFSET
        num_ports = self.max_port - self.min_port + 1
        port_offset = shard % num_ports
        host_id = (shard - port_offset) // num_ports

        port = self.min_port + port_offset
        addr = self.subnet.network_address + host_id
        if addr not in self.subnet:
            raise ValueError("The shard is not valid")
        return Address(str(addr), port)

    def find_free_port(self, address):
        """Find the first open port.

        address (IPv4Address|IPv6Address): local address to bind to
        """
        if address.version == 4:
            family = socket.AF_INET
        else:
            family = socket.AF_INET6
        for port in range(self.min_port, self.max_port+1):
            with closing(socket.socket(family, socket.SOCK_STREAM)) as sock:
                try:
                    sock.bind((str(address), port))
                    return port
                except socket.error:
                    continue
        raise ValueError("No free port found in range [%s, %s] "
                        "for address %s" % (self.min_port, self.max_port, \
                        address))


class AsyncConfig:
    """This class will contain the configuration for the
    services. This needs to be populated at the initilization stage.

    The *_services variables are dictionaries indexed by ServiceCoord
    with values of type Address.

    Core services are the ones that are supposed to run whenever the
    system is up.

    Other services are not supposed to run when the system is up, or
    anyway not constantly.

    """
    core_services = {}
    other_services = {}
    ephemeral_services = {}  # type: Dict[str, EphemeralServiceConfig]


async_config = AsyncConfig()


class Config:
    """This class will contain the configuration for CMS. This needs
    to be populated at the initilization stage. This is loaded by
    default with some sane data. See cms.conf.sample in the config
    directory for information on the meaning of the fields.

    """
    def __init__(self):
        """Default values for configuration, plus decide if this
        instance is running from the system path or from the source
        directory.

        """
        self.async_config = async_config

        # System-wide
        self.cmsuser = "cmsuser"
        self.temp_dir = "/tmp"
        self.backdoor = False
        self.file_log_debug = False
        self.stream_log_detailed = False

        # Database.
        self.database = "postgresql+psycopg2://cmsuser@localhost/cms"
        self.database_debug = False
        self.twophase_commit = False

        # Worker.
        self.keep_sandbox = False
        self.use_cgroups = True
        self.sandbox_implementation = 'isolate'
        self.store_exe_in_db = True

        # Sandbox.
        # Max size of each writable file during an evaluation step, in KiB.
        self.max_file_size = 1024 * 1024  # 1 GiB
        # Number of boxes supported by isolate, see /usr/local/etc/isolate
        self.num_boxes = 1000
        # Max processes, CPU time (s), memory (KiB) for compilation runs.
        self.compilation_sandbox_max_processes = 1000
        self.compilation_sandbox_max_time_s = 10.0
        self.compilation_sandbox_max_memory_kib = 512 * 1024  # 512 MiB
        # Max processes, CPU time (s), memory (KiB) for trusted runs.
        self.trusted_sandbox_max_processes = 1000
        self.trusted_sandbox_max_time_s = 10.0
        self.trusted_sandbox_max_memory_kib = 4 * 1024 * 1024  # 4 GiB

        # WebServers.
        self.secret_key_default = "8e045a51e4b102ea803c06f92841a1fb"
        self.secret_key = self.secret_key_default
        self.tornado_debug = False

        # ContestWebServer.
        self.contest_listen_address = [""]
        self.contest_listen_port = [8888]
        self.cookie_duration = 30 * 60  # 30 minutes
        self.submit_local_copy = True
        self.submit_local_copy_path = "%s/submissions/"
        self.tests_local_copy = True
        self.tests_local_copy_path = "%s/tests/"
        self.is_proxy_used = None  # (deprecated in favor of num_proxies_used)
        self.num_proxies_used = None
        self.max_submission_length = 100_000  # 100 KB
        self.max_input_length = 5_000_000  # 5 MB
        self.stl_path = "/usr/share/cppreference/doc/html/"
        self.docs_path = None
        # Prefix of 'shared-mime-info'[1] installation. It can be found
        # out using `pkg-config --variable=prefix shared-mime-info`, but
        # it's almost universally the same (i.e. '/usr') so it's hardly
        # necessary to change it.
        # [1] http://freedesktop.org/wiki/Software/shared-mime-info
        self.shared_mime_info_prefix = "/usr"
        self.telegram_bot_token = None
        self.telegram_bot_chat_id = None

        # AdminWebServer.
        self.admin_listen_address = ""
        self.admin_listen_port = 8889
        self.admin_cookie_duration = 10 * 60 * 60  # 10 hours
        self.admin_num_proxies_used = None

        # ProxyService.
        self.rankings = ["http://usern4me:passw0rd@localhost:8890/"]
        self.https_certfile = None

        # PrintingService
        self.max_print_length = 10_000_000  # 10 MB
        self.printer = None
        self.paper_size = "A4"
        self.max_pages_per_job = 10
        self.max_jobs_per_user = 10
        self.pdf_printing_allowed = False

        # Installed or from source?
        # We declare we are running from installed if the program was
        # NOT invoked through some python flavor, and the file is in
        # the prefix (or real_prefix to accommodate virtualenvs).
        bin_path = os.path.join(os.getcwd(), sys.argv[0])
        bin_name = os.path.basename(bin_path)
        bin_is_python = bin_name in ["ipython", "python", "python3"]
        bin_in_installed_path = bin_path.startswith(sys.prefix) or (
            hasattr(sys, 'real_prefix')
            and bin_path.startswith(sys.real_prefix))
        self.installed = bin_in_installed_path and not bin_is_python

        if self.installed:
            self.log_dir = os.path.join("/", "var", "local", "log", "cms")
            self.cache_dir = os.path.join("/", "var", "local", "cache", "cms")
            self.data_dir = os.path.join("/", "var", "local", "lib", "cms")
            self.run_dir = os.path.join("/", "var", "local", "run", "cms")
            paths = [os.path.join("/", "usr", "local", "etc", "cms.conf"),
                     os.path.join("/", "etc", "cms.conf")]
        else:
            self.log_dir = "log"
            self.cache_dir = "cache"
            self.data_dir = "lib"
            self.run_dir = "run"
            paths = [os.path.join(".", "config", "cms.conf")]
            if '__file__' in globals():
                paths += [os.path.abspath(os.path.join(
                          os.path.dirname(__file__),
                          '..', 'config', 'cms.conf'))]
            paths += [os.path.join("/", "usr", "local", "etc", "cms.conf"),
                      os.path.join("/", "etc", "cms.conf")]

        # Allow user to override config file path using environment
        # variable 'CMS_CONFIG'.
        CMS_CONFIG_ENV_VAR = "CMS_CONFIG"
        if CMS_CONFIG_ENV_VAR in os.environ:
            paths = [os.environ[CMS_CONFIG_ENV_VAR]] + paths

        # Attempt to load a config file.
        self._load(paths)

        # Load from environment variables
        self._load_from_env()

        if bool(self.telegram_bot_token) ^ bool(self.telegram_bot_chat_id):
            raise ConfigError("Both telegram_bot_token and telegram_bot_chat_id "
                              "should be set or left null")
        if self.telegram_bot_chat_id:
            if type(self.telegram_bot_chat_id) != int:
                raise ConfigError("telegram_bot_chat_id should be an integer")

        # If the configuration says to print detailed log on stdout,
        # change the log configuration.
        set_detailed_logs(self.stream_log_detailed)

    def _load(self, paths):
        """Try to load the config files one at a time, until one loads
        correctly.

        """
        for conf_file in paths:
            if self._load_unique(conf_file):
                break
        else:
            logging.warning("No configuration file found: "
                            "falling back to default values.")

    def _load_unique(self, path):
        """Populate the Config class with everything that sits inside
        the JSON file path (usually something like /etc/cms.conf). The
        only pieces of data treated differently are the elements of
        core_services and other_services that are sent to async
        config.

        Services whose name begins with an underscore are ignored, so
        they can be commented out in the configuration file.

        path (string): the path of the JSON config file.

        """
        # Load config file.
        try:
            with open(path, 'rt', encoding='utf-8') as f:
                data = pyjson5.load(f)
        except FileNotFoundError:
            logger.debug("Couldn't find config file %s.", path)
            return False
        except OSError as error:
            logger.warning("I/O error while opening file %s: [%s] %s",
                           path, errno.errorcode[error.errno],
                           os.strerror(error.errno))
            return False
        except ValueError as error:
            logger.warning("Invalid syntax in file %s: %s", path, error)
            return False

        logger.info("Using configuration file %s.", path)

        if "is_proxy_used" in data:
            logger.warning("The 'is_proxy_used' setting is deprecated, please "
                           "use 'num_proxies_used' instead.")

        # Put core and test services in async_config, ignoring those
        # whose name begins with "_".
        for service in data["core_services"]:
            if service.startswith("_"):
                continue
            for shard_number, shard in \
                    enumerate(data["core_services"][service]):
                coord = ServiceCoord(service, shard_number)
                self.async_config.core_services[coord] = Address(*shard)
        del data["core_services"]

        for service in data["other_services"]:
            if service.startswith("_"):
                continue
            for shard_number, shard in \
                    enumerate(data["other_services"][service]):
                coord = ServiceCoord(service, shard_number)
                self.async_config.other_services[coord] = Address(*shard)
        del data["other_services"]

        for service_name in data["ephemeral_services"]:
            if service_name.startswith("_"):
                continue
            service = data["ephemeral_services"][service_name]
            self.async_config.ephemeral_services[service_name] = \
                EphemeralServiceConfig(
                    service["subnet"],
                    service["min_port"],
                    service["max_port"],
                )
        del data["ephemeral_services"]

        # Put everything else in self.
        for key, value in data.items():
            setattr(self, key, value)

        return True

    def _load_from_env(self):
        variables = [
            ("CMS_DATABASE", "database"),
            ("CMS_SECRET_KEY", "secret_key"),
        ]
        for variable, key in variables:
            if variable in os.environ:
                setattr(self, key, os.environ[variable])

        files = [
            ("CMS_DATABASE_FILE", "database"),
            ("CMS_SECRET_KEY_FILE", "secret_key"),
        ]
        for variable, key in files:
            if variable in os.environ:
                with open(os.environ[variable], "r") as f:
                    setattr(self, key, f.read().strip())

config = Config()
