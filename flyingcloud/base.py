#!/usr/bin/env python
# -*- coding: utf-8 -*-


import argparse
import datetime
import glob
import json
import tempfile

import docker
import logging

import io
import os
import platform

import psutil
import requests

import re
import sh
import time

from .utils import disk_usage, abspath

STREAMING_CHUNK_SIZE = (1 << 20)


# TODO
# - do a better job of logging container-ids and image-ids
# - unit tests, using a mock docker-py

class FlyingCloudError(Exception):
    """Base error"""


class EnvironmentVarError(FlyingCloudError):
    """Missing environment variable"""


class NotSudoError(FlyingCloudError):
    """Not running as root"""


class CommandError(FlyingCloudError):
    """Command failure"""


class ExecError(FlyingCloudError):
    """Failure to run a command in Docker container"""


class DockerResultError(FlyingCloudError):
    """Error in result from Docker Daemon"""


class DockerBuildLayer(object):
    """Build a Docker image using SaltStack

    Can either build from a base image or from a Dockerfile.
    Uses Salt states to build each layer.
    Finished layers are pushed to the registry.
    """
    # Override these as necessary
    SaltExecTimeout = 45 * 60  # seconds, for long-running commands
    DefaultTimeout = 5 * 60  # need longer than default timeout for most commands

    USERNAME_ENV_VAR = 'FLYINGCLOUD_DOCKER_REGISTRY_USERNAME'
    PASSWORD_ENV_VAR = 'FLYINGCLOUD_DOCKER_REGISTRY_PASSWORD'

    RegistryConfig = dict(
        host=None,
        organization=None,
        docker_api_version=None,
        login_required=True,
        pull_layer=True,
        push_layer=False,
        squash_layer=False,
    )

    def __init__(
            self,
            app_name,
            layer_name,
            source_image_base_name,
            help,
            description=None,
            exposed_ports=None,
            registry_config=None,
            source_version_tag="latest"
    ):
        self.app_name = app_name
        self.layer_name = layer_name
        self.source_image_base_name = source_image_base_name
        self.help = help
        self.description = description
        self.exposed_ports = exposed_ports or []

        config = self.RegistryConfig.copy()
        if registry_config:
            config.update(registry_config)
        self.registry_config = config

        host, org = config['host'], config['organization']
        host_org = "{}/{}/".format(host, org) if host and org else ""

        self.container_name = "{}_{}".format(self.app_name, self.layer_name)
        self.docker_layer_name = "{}{}".format(host_org, self.container_name)
        self.layer_latest_name = "{}:latest".format(self.docker_layer_name)

        if source_image_base_name:
            self.source_image_name = "{}{}:{}".format(
                host_org, self.source_image_base_name, source_version_tag)
        else:
            self.source_image_name = None

        # These require the command-line args to properly initialize
        self.layer_timestamp_name = self.layer_squashed_name = None

    def main(self, defaults, *layer_classes, **kwargs):
        self.check_user_is_root()
        self.check_environment_variables()
        namespace = self.parse_args(defaults, *layer_classes, **kwargs)
        self.do_operation(namespace)

    @classmethod
    def check_user_is_root(cls):
        if platform.system() == "Linux" and os.geteuid() != 0 :
            raise NotSudoError("You must be root (use sudo)")

    def check_environment_variables(self):
        if self.registry_config['host'] and self.registry_config['login_required']:
            for v in [self.USERNAME_ENV_VAR, self.PASSWORD_ENV_VAR]:
                if v not in os.environ:
                    raise EnvironmentVarError("Environment variable {} not defined".format(v))

    def do_operation(self, namespace):
        method = getattr(self, 'do_' + namespace.operation)
        return method(namespace)

    def do_run(self, namespace):
        self.port_forwarding(namespace)
        target_container_name = self.docker_create_container(
            namespace,
            self.container_name,
            self.layer_latest_name)
        self.docker_start(namespace, target_container_name)

    def do_kill(self, namespace):
        try:
            self.docker_cleanup(namespace, self.container_name)
        except (docker.errors.DockerException, docker.errors.APIError):
            pass
        self.kill_port_forwarding(namespace)

    def do_build(self, namespace):
        namespace.logger.info("Build starting...")
        self.log_disk_usage(namespace)
        self.docker_info(namespace)
        if self.should_build(namespace):
            self.build(namespace)
        namespace.logger.info("Build finished")

    def should_build(self, namespace):
        return True

    def initialize_build(self, namespace, salt_dir):
        """Override if you need special handling"""
        pass

    def build(self, namespace):
        salt_dir = os.path.abspath(os.path.join(namespace.salt_dir, self.layer_name))

        if not os.path.exists(salt_dir):
            message = "Configuration directory %s does not exist, failing!" % salt_dir
            namespace.logger.error("%s", message)
            raise CommandError(message)

        self.layer_timestamp_name = "{}:{}".format(self.docker_layer_name, namespace.timestamp)
        self.layer_squashed_name = "{}-sq".format(self.layer_timestamp_name)

        self.initialize_build(namespace, salt_dir)

        if namespace.push_layer and self.registry_config['pull_layer']:
            self.docker_pull(namespace, self.source_image_name)

        dockerfile = self.get_dockerfile(salt_dir)
        if dockerfile:
            namespace.logger.info("Building %s", dockerfile)
            self.source_image_name = self.build_dockerfile(
                namespace, tag=self.layer_timestamp_name, dockerfile=dockerfile)
        else:
            self.make_expose_ports(namespace)

        target_container_name = self.salt_highstate(
            namespace, self.container_name,
            source_image_name=self.source_image_name,
            result_image_name=self.layer_timestamp_name,
            salt_dir=salt_dir)

        layer_strong_name = None
        if namespace.squash_layer and self.registry_config['squash_layer']:
            layer_strong_name = self.docker_squash(
                namespace,
                image_name=self.layer_timestamp_name,
                latest_image_name=self.layer_latest_name,
                squashed_image_name=self.layer_squashed_name)
            remove_layer = self.layer_timestamp_name
        if layer_strong_name is None:
            layer_strong_name = self.layer_timestamp_name
            namespace.logger.info("Not squashing layer %s", layer_strong_name)
            remove_layer = None
            self.docker_tag(namespace, layer_strong_name, "latest")

        # TODO: make the following lines work consistently; on some Linux boxes, they don't work
        # if remove_layer:
        #     self.docker_remove_image(namespace, remove_layer)
        if namespace.push_layer and self.registry_config['push_layer']:
            self.docker_push(
                namespace,
                layer_strong_name)
            self.docker_push(
                namespace,
                self.layer_latest_name)
        else:
            namespace.logger.info("Not pushing Docker layers.")

        return layer_strong_name

    def salt_highstate(
            self,
            namespace,
            container_name,
            source_image_name,
            result_image_name,
            salt_dir, timeout=SaltExecTimeout):
        """Use SaltStack to configure container"""
        if not self.salt_states_exist(salt_dir):
            namespace.logger.info("No salt states found in '%s'; not salting.", salt_dir)
            return None

        namespace.logger.info(
            "Starting salt_highstate: source_image_name=%s, container_name=%s, salt_dir=%s",
            source_image_name, container_name, salt_dir)
        try:
            target_container_name = self.docker_create_container(
                namespace, container_name, source_image_name,
                volume_map={salt_dir: "/srv/salt"})

            self.docker_start(namespace, target_container_name)

            namespace.logger.info("About to start Salting")
            start_time = time.time()
            result, salt_output = self.docker_exec(
                namespace, target_container_name,
                ["salt-call", "--local", "state.highstate"],
                timeout)
            duration = round(time.time() - start_time)
            namespace.logger.info(
                "Finished Salting: duration=%d:%02d minutes", duration // 60, duration % 60)
            if self.salt_error(salt_output):
                raise ExecError("salt_highstate failed.")

            result = self.docker_commit(namespace, target_container_name, result_image_name)
            namespace.logger.info("Committed: %r", result)
        except:
            namespace.logger.exception("Salting failed")
            raise
        finally:
            self.docker_cleanup(namespace, target_container_name)
        return target_container_name

    def salt_states_exist(self, salt_dir):
        files = glob.glob(os.path.join(salt_dir, '*.sls'))
        return len(files)

    def salt_error(self, salt_output):
        return re.search("\s*Failed:\s+[1-9]\d*\s*$", salt_output, re.MULTILINE) is not None

    def get_dockerfile(self, salt_dir):
        df = os.path.join(salt_dir, "Dockerfile")
        return df if os.path.exists(df) else None

    def make_expose_ports(self, namespace):
        if self.exposed_ports:
            port_list = " ".join(str(p) for p in self.container_ports(self.exposed_ports))
            Dockerfile = """\
                FROM {}
                EXPOSE {}
            """.format(self.source_image_name, port_list)
            namespace.logger.info("Exposing ports: %s", port_list)
            with io.BytesIO(Dockerfile.encode('utf-8')) as fileobj:
                return self.build_dockerfile(
                    namespace, tag=self.layer_timestamp_name, fileobj=fileobj)

    @classmethod
    def container_ports(cls, exposed_ports):
        ports = []
        for p in exposed_ports:
            if isinstance(p, dict):
                assert len(p) == 1
                container_ports = p.values()[0]
                if not isinstance(container_ports, list):
                    container_ports = [container_ports]
            else:
                container_ports = [p]
            ports.extend(int(cp) for cp in container_ports)
        return ports

    @classmethod
    def host_ports(cls, exposed_ports):
        ports = []
        for p in exposed_ports:
            if isinstance(p, dict):
                assert len(p) == 1
                host_port = p.keys()[0]
            else:
                host_port = p
            ports.append(int(host_port))
        return ports

    @classmethod
    def port_bindings(cls, exposed_ports):
        pb = {}
        for p in exposed_ports:
            if isinstance(p, dict):
                host_port, container_ports = p.items()[0]
                if not isinstance(container_ports, list):
                    container_ports = [container_ports]
            else:
                host_port = p
                container_ports = [p]
            for cp in container_ports:
                pb[int(cp)] = int(host_port)
        return pb

    def port_forwarding(self, namespace):
        if self.use_docker_machine():
            for host_port in self.host_ports(self.exposed_ports):
                ssh_args = self.ssh_port_forward_args(host_port)
                process = self.find_port_forwarding(namespace, ssh_args)
                if process:
                    namespace.logger.info("Already forwarding %d: PID=%d",
                                          host_port, process.pid)
                else:
                    args = ["ssh", namespace.docker_machine_name] + ssh_args
                    namespace.logger.info("port_forwarding: %r", args)
                    result = self.docker_machine(*args, _bg=True)
                    namespace.logger.info("port_forwarded: %r", result)

    def kill_port_forwarding(self, namespace):
        if self.use_docker_machine():
            for host_port in self.host_ports(self.exposed_ports):
                ssh_args = self.ssh_port_forward_args(host_port)
                process = self.find_port_forwarding(namespace, ssh_args)
                if process:
                    namespace.logger.info(
                        "Killing port forwarding for %d: PID=%d", host_port, process.pid)
                    process.kill()

    def ssh_port_forward_args(self, host_port):
        return ["-f", "-N", "-L", "{0}:localhost:{0}".format(host_port)]

    def find_port_forwarding(self, namespace, args):
        for process in psutil.process_iter():
            try:
                if process.name().endswith('ssh'):
                    if process.cmdline()[-len(args):] == args:
                        return process
            except psutil.NoSuchProcess:
                pass

    def build_dockerfile(self, namespace, tag, dockerfile=None, fileobj=None):
        namespace.logger.info("About to build Dockerfile, tag=%s", tag)
        if dockerfile:
            dockerfile = os.path.relpath(dockerfile, namespace.base_dir)
        for line in namespace.docker.build(tag=tag, path=namespace.base_dir,
                                           dockerfile=dockerfile, fileobj=fileobj):
            line = line.rstrip('\r\n')
            namespace.logger.debug("%s", line)
        # Grrr! Why doesn't docker-py handle this for us?
        match = re.search(r'Successfully built ([0-9a-f]+)', line)
        image_id = match and match.group(1)
        namespace.logger.info("Built tag=%s, image_id=%s", tag, image_id)
        return image_id

    def docker_create_container(
            self, namespace, container_name, image_name,
            environment=None, detach=True, volume_map=None, **kwargs):
        namespace.logger.info("Creating container '%s' from image %s",
                              container_name, image_name)
        namespace.logger.debug(
            "Tags for image '%s': %s",
            image_name, self.docker_tags_for_image(namespace, image_name))

        kwargs['image'] = image_name
        kwargs['name'] = container_name
        kwargs['environment'] = environment
        kwargs['detach'] = detach
        kwargs['ports'] = self.container_ports(self.exposed_ports)
        kwargs.update(self.docker_host_config(namespace, volume_map))
        namespace.logger.info("create_container: %r", kwargs)

        container = namespace.docker.create_container(**kwargs)
        container_id = container['Id']
        namespace.logger.info("Created container %s, result=%r", container_id[:12], container)
        return container_id

    def docker_host_config(self, namespace, volume_map, mode='rw'):
        volumes, binds = [], []
        for local_path, remote_path in (volume_map or {}).items():
            volumes.append(remote_path)
            binds.append("{}:{}:{}".format(
                os.path.abspath(local_path), remote_path, mode))
        return dict(
            volumes=volumes or None,
            host_config=namespace.docker.create_host_config(
                binds=binds,
                port_bindings=self.port_bindings(self.exposed_ports))
        )

    def log_disk_usage(self, namespace, *extra_paths):
        for path in (
                '/',
                abspath('~/.docker'),
                '/var/lib/docker',
                tempfile.gettempdir()) + extra_paths:
            if os.path.exists(path):
                namespace.logger.info("Disk Usage '%s': %r", path, disk_usage(path))

    def docker_tags_for_image(self, namespace, image_name):
        parts = image_name.split('/')
        if len(parts) == 3 and namespace.username and namespace.password:
            url = "https://{0}/v1/repositories/{1}/{2}/tags".format(
                parts[0], parts[1], parts[2].split(':')[0])
            r = requests.get(url, auth=(namespace.username, namespace.password))
            return r.json()

    def docker_start(self, namespace, container_id, **kwargs):
        return namespace.docker.start(container_id, **kwargs)

    def docker_exec(self, namespace, container_id, cmd, timeout=None, raise_on_error=True):
        exec_id = self.docker_exec_create(namespace, container_id, cmd)
        return self.docker_exec_start(namespace, exec_id, timeout, raise_on_error)

    def docker_exec_create(self, namespace, container_id, cmd):
        namespace.logger.info("Running %r in container %s", cmd, container_id[:12])
        exec_create = namespace.docker.exec_create(container=container_id, cmd=cmd)
        return exec_create['Id']

    def docker_exec_start(self, namespace, exec_id, timeout=None, raise_on_error=True):
        timeout = timeout or namespace.timeout or self.SaltExecTimeout
        # Use a distinct client with a custom timeout
        # (synchronous execs can last much longer than 60 seconds)
        client = self.docker_client(namespace, timeout=timeout)
        generator = client.exec_start(exec_id=exec_id, stream=True)
        full_output = self.read_docker_output_stream(namespace, generator, "docker_exec")
        result = client.exec_inspect(exec_id=exec_id)
        exit_code = result['ExitCode']
        if exit_code != 0 and raise_on_error:
            raise ExecError("docker_exec exit code was non-zero: {} (result: {})".format(exit_code, result))
        return result, full_output

    def read_docker_output_stream(self, namespace, generator, logger_prefix):
        full_output = []
        for chunk in generator:
            full_output.append(chunk)
            try:
                data = json.loads(chunk)
            except ValueError:
                data = chunk.rstrip('\r\n')
            namespace.logger.debug("%s: %s", logger_prefix, data)
            if isinstance(data, dict) and 'error' in data:
                raise DockerResultError("Error: {!r}".format(data))
        return '\n'.join(full_output)

    def docker_commit(self, namespace, container_id, result_image_name):
        repo, tag = self.image_name2repo_tag(result_image_name)
        return namespace.docker.commit(container=container_id, repository=repo, tag=tag)

    def find_binary(self, namespace, filename, search_paths=None):
        if search_paths is None:
            search_paths = [os.environ.get('VIRTUAL_ENV'), '/usr/local']
            search_paths = [os.path.join(p, "bin") for p in search_paths if p]
        for path in search_paths:
            filepath = os.path.join(path, filename)
            if os.path.exists(filepath):
                return filepath
        namespace.logger.info("Can't find '%s' in %s", filename, search_paths)
        return None

    def docker_squash(self, namespace, image_name, latest_image_name, squashed_image_name):
        docker_squash_path = self.find_binary(namespace, 'docker-squash')
        if docker_squash_path is None:
            namespace.logger.info("Not squashing")
            return None
        else:
            namespace.logger.info("Using %s", docker_squash_path)
        docker_squash_cmd = sh.Command(docker_squash_path)

        self.log_disk_usage(namespace)
        try:
            input_temp = tempfile.NamedTemporaryFile(suffix="-input-image.tar", delete=False)
            output_temp = tempfile.NamedTemporaryFile(suffix="-output-image.tar", delete=False)
            # docker save to tarfile
            image_raw = namespace.docker.get_image(image_name)
            for chunk in image_raw.stream(STREAMING_CHUNK_SIZE, decode_content=True):
                input_temp.write(chunk)
            input_temp.close()

            # docker-squash -i tar1 -o tar2
            # TODO: use subprocess.Popen and pipe input and output
            output_temp.close()
            namespace.logger.info("Squashing '%s' (%d bytes) to '%s'",
                                  input_temp.name, os.path.getsize(input_temp.name), output_temp.name)
            docker_squash_cmd("-i", input_temp.name, "-o", output_temp.name, "-t", latest_image_name,
                              "-from", "root")
            output_temp = open(output_temp.name, 'rb')

            # docker load tar2
            namespace.logger.info("Loading squashed image (%d bytes)", os.path.getsize(output_temp.name))
            namespace.docker.load_image(data=output_temp)
            output_temp.close()

            _, tag = self.image_name2repo_tag(squashed_image_name)
            self.docker_tag(namespace, latest_image_name, tag=tag)
        finally:
            os.unlink(input_temp.name)
            os.unlink(output_temp.name)

        return squashed_image_name

    def docker_get_strong_name_of_latest_image(self, namespace, image_name):
        images = namespace.docker.images()
        latest_image_name = image_name + ":latest"
        for image in images:
            repo_tags = set(image['RepoTags'])
            namespace.logger.info("repo tags: %r", repo_tags)
            if latest_image_name in repo_tags:
                repo_tags.remove(latest_image_name)
                result = repo_tags.pop()
                if result:
                    return result
                else:
                    return latest_image_name

    # TODO: cleanly remove all non-running containers
    def docker_cleanup(self, namespace, container_name):
        namespace.logger.info("docker_cleanup %s", container_name)
        self.docker_stop(namespace, container_name)
        self.docker_remove_container(namespace, container_name)

    def docker_stop(self, namespace, container_name):
        namespace.docker.stop(container_name)

    def docker_kill(self, namespace, container_name, signal=None):
        return namespace.docker.kill(container_name, signal=signal)

    def docker_remove_container(self, namespace, container_name, force=True):
        namespace.docker.remove_container(container=container_name, force=force)

    def docker_remove_image(self, namespace, image_name, force=True):
        namespace.docker.remove_image(image=image_name, force=force)

    def image_name2repo_tag(self, image_name, tag=None):
        repo, image_tag = image_name.split(':')
        tag = tag or image_tag
        return repo, tag

    def docker_tag(self, namespace, image_name, tag=None, force=True):
        repo, tag = self.image_name2repo_tag(image_name, tag)
        namespace.logger.info("Tagging image %s as repo=%s, tag=%s", image_name, repo, tag)
        namespace.docker.tag(image=image_name, repository=repo, tag=tag, force=force)

    def docker_pull(self, namespace, image_name):
        return self._docker_push_pull(namespace, image_name, "pull")

    def docker_push(self, namespace, image_name):
        return self._docker_push_pull(namespace, image_name, "push")

    def _docker_push_pull(self, namespace, image_name, verb):
        give_up_message = "Couldn't {} {}. Giving up after {} attempts.".format(
            verb, image_name, namespace.retries)
        for attempt in range(1, namespace.retries + 1):
            try:
                namespace.logger.info("docker_%s %s, attempt %d/%d",
                                      verb, image_name, attempt, namespace.retries)
                repo, tag = self.image_name2repo_tag(image_name)
                method = getattr(namespace.docker, verb)
                generator = method(repository=repo, tag=tag, stream=True)
                return self.read_docker_output_stream(
                    namespace, generator, "docker_{}".format(verb))
            except DockerResultError:
                if attempt == namespace.retries:
                    namespace.logger.info("%s", give_up_message)
                    raise
        else:
            raise DockerResultError(give_up_message)

    def docker_login(self, namespace, username, password, registry):
        if registry:
            if username and password:
                namespace.logger.info(
                    "Logging in to registry '%s' as user '%s'",
                    registry, namespace.username)
                return namespace.docker.login(
                    username=username,
                    password=password,
                    registry=registry)
            elif self.registry_config['login_required']:
                assert username, "No username"
                assert password, "No password"

    def docker_info(self, namespace):
        info = namespace.docker.info()
        namespace.logger.info("Docker Info: %r", info)
        return info

    def configure_logging(self, namespace):
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.DEBUG)
        fh = logging.FileHandler(namespace.logfile)
        fh.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG if namespace.debug else logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        ch.setFormatter(formatter)
        fh.setFormatter(formatter)
        logger.addHandler(ch)
        logger.addHandler(fh)
        return logger

    def add_additional_configuration(self, namespace):
        """Override to add additional configuration to namespace"""
        pass

    def parse_args(self, defaults, *layer_classes, **kwargs):
        parser = argparse.ArgumentParser(
            description=kwargs.pop('description', "Build a Docker image using SaltStack"))

        defaults = defaults or {}
        defaults.setdefault('base_dir', os.path.abspath(os.path.dirname(__file__)))
        defaults.setdefault('salt_dir', os.path.join(defaults['base_dir'], "salt"))
        defaults.setdefault('logfile', os.path.join(defaults['base_dir'], "flyingcloud.log"))
        defaults.setdefault('timestamp_format', '%Y-%m-%dt%H%M%Sz')
        defaults.setdefault(
            'timestamp',
            datetime.datetime.utcnow().strftime(defaults['timestamp_format']))
        defaults.setdefault('pull_layer', True)
        defaults.setdefault('push_layer', True)
        defaults.setdefault('squash_layer', True)
        defaults.setdefault('retries', 3)
        defaults.setdefault('docker_machine_name',
                            os.environ.get('DOCKER_MACHINE_NAME', 'default'))
        defaults.setdefault('layer_inst', self)
        defaults.setdefault('operation', 'build')

        parser.set_defaults(**defaults)

        parser.add_argument(
            '--timeout', '-t', type=int, default=self.DefaultTimeout,
            help="Docker client timeout in seconds. Default: %(default)s")
        parser.add_argument(
            '--no-pull', '-p', dest='pull_layer', action='store_false',
            help="Do not pull Docker image from repository")
        parser.add_argument(
            '--no-push', '-P', dest='push_layer', action='store_false',
            help="Do not push Docker image to repository")
        parser.add_argument(
            '--no-squash', '-S', dest='squash_layer', action='store_false',
            help="Do not squash Docker image")
        parser.add_argument(
            '--retries', '-R', dest='retries', type=int,
            help="How often to retry remote Docker operations, such as push/pull. "
                 "Default: %(default)d")
        parser.add_argument(
            '--debug', '-D', dest='debug', action='store_true',
            help="Set terminal logging level to DEBUG, etc")
        if self.use_docker_machine():
            parser.add_argument(
                '--docker-machine-name', '-M',
                help="Name of machine to use with docker-machine. Default: '%(default)s'")

        op_group = parser.add_argument_group("Operations")
        op_group = op_group.add_mutually_exclusive_group()
        op_group.add_argument(
            '--build', '-b', dest='operation', action='store_const', const='build',
            help="Build a layer. (Default)")
        op_group.add_argument(
            '--run', '-r', dest='operation', action='store_const', const='run',
            help="Run a layer.")
        op_group.add_argument(
            '--kill', '-k', dest='operation', action='store_const', const='kill',
            help="Kill a running layer.")

        subparsers = parser.add_subparsers(
            title="Layer Names",
            description="The layers which can be built, run, or killed.")

        for layer_class_or_inst in layer_classes:
            if type(layer_class_or_inst).__name__ == 'classobj':
                layer_inst = layer_class_or_inst()
            else:
                layer_inst = layer_class_or_inst
            subparser = subparsers.add_parser(
                layer_inst.layer_name,
                description=layer_inst.description,
                help=layer_inst.help)
            subparser.set_defaults(
                layer_inst=layer_inst,
            )
            layer_inst.add_parser_options(subparser)

        namespace = parser.parse_args()

        namespace.logger = self.configure_logging(namespace)
        namespace.username = os.environ.get(self.USERNAME_ENV_VAR)
        namespace.password = os.environ.get(self.PASSWORD_ENV_VAR)
        namespace.docker = self.docker_client(namespace, timeout=namespace.timeout)

        self.docker_login(
            namespace,
            namespace.username,
            namespace.password,
            registry=self.registry_config['host'])

        self.add_additional_configuration(namespace)

        return namespace

    @classmethod
    def add_parser_options(cls, subparser):
        pass

    def docker_client(self, namespace, *args, **kwargs):
        namespace.logger.info("Platform is '%s'.", platform.system())
        kwargs.setdefault('timeout', self.DefaultTimeout)
        if self.registry_config['docker_api_version']:
            kwargs.setdefault('version', self.registry_config['docker_api_version'])
        if self.use_docker_machine():
            kwargs = self.get_docker_machine_client(namespace, **kwargs)
        namespace.logger.debug("Constructing docker client object with %s", kwargs)
        return docker.Client(*args, **kwargs)

    @classmethod
    def use_docker_machine(cls):
        # TODO: Windows
        return platform.system() == "Darwin"

    def docker_machine(self, *args, **kwargs):
        with io.BytesIO() as output:
            cmd = sh.Command("docker-machine")
            cmd(*args,_out=output, **kwargs)
            return output.getvalue()

    def get_docker_machine_client(self, namespace, **kwargs):
        # TODO: better error handling
        docker_machine_json = self.docker_machine("inspect", namespace.docker_machine_name)
        namespace.logger.debug("docker-machine json: %r", docker_machine_json)
        namespace.logger.debug("docker-machine json type: %r", type(docker_machine_json))
        docker_machine_json = json.loads(docker_machine_json)
        docker_machine_tls = docker_machine_json['HostOptions']['AuthOptions']
        docker_machine_ip = docker_machine_json['Driver']['IPAddress']
        # Use docker-s port. TODO: IPv6?
        kwargs['base_url'] = 'https://' + docker_machine_ip + ':2376'
        kwargs['tls'] = docker.tls.TLSConfig(
            client_cert=(docker_machine_tls['ClientCertPath'],
                         docker_machine_tls['ClientKeyPath']),
            ca_cert=docker_machine_tls['CaCertPath'],
            assert_hostname=False,
            verify=True)
        namespace.logger.info(
            "Docker-Machine ('%s'): using %r", namespace.docker_machine_name, kwargs)
        return kwargs
