# Licensed under LICENSE.md; also available at https://www.prefect.io/licenses/alpha-eula

"""
Environments in the Prefect library serve as containers capable of serializing, loading and executing
flows. Currently, available environments consist of a Docker `ContainerEnvironment`
and a `LocalEnvironment`.

Environments will be a crucial element in the execution lifecycle for a Flow
through the Prefect Server.

**Note:** Due to ongoing development this file is subject to large changes.
"""

import subprocess
import tempfile
import textwrap
from typing import Any, Iterable

import docker
from cryptography.fernet import Fernet

import prefect
from prefect.utilities.json import ObjectAttributesCodec, Serializable


class Secret(Serializable):
    """
    A Secret is a serializable object used to represent a secret key & value that is
    to live inside of an environment.

    Args:
        - name (str): The name of the secret to be put into the environment

    The value of the Secret is not set upon initialization and instead functions as a
    property of the Secret. e.g. `my_secret.value = "1234"`
    """

    _json_codec = ObjectAttributesCodec

    def __init__(self, name: str) -> None:
        self.name = name
        self._value = None

    @property
    def value(self) -> Any:
        """Get the secret's value"""
        return self._value

    @value.setter
    def value(self, value: Any) -> None:
        """Set the secret's value"""
        self._value = value


class Environment(Serializable):
    """
    Base class for Environments
    """

    _json_codec = ObjectAttributesCodec

    def __init__(self, secrets: Iterable[Secret] = None) -> None:
        self.secrets = secrets or []

    def build(self, flow: "prefect.Flow") -> bytes:
        """
        Build the environment. Returns a key that must be passed to interact with the
        environment.

        Args:
            - flow (prefect.Flow): the Flow to build the environment for

        Returns:
            - bytes: a key required for interacting with the environment
        """
        raise NotImplementedError()

    def run(self, key: bytes, cli_cmd: str) -> int:
        """Issue a CLI command to the environment.

        Args:
            - key (bytes): the environment key
            - cli_cmd (str): the command to issue
        """
        raise NotImplementedError()


class ContainerEnvironment(Environment):
    """
    Container class used to represent a Docker container.

    **Note:** this class is still experimental, does not fully support all functions,
    and is subject to change.

    Args:
        - image (str): The image to pull that is used as a base for the Docker container
        (*Note*: An image that is provided must be able to handle `python` and `pip` commands)
        - tag (str): The tag for this container
        - python_dependencies (list, optional): The list of pip installable python packages
        that will be installed on build of the Docker container
        - secrets ([Secret], optional): Iterable list of Secrets that will be used as
        environment variables in the Docker container
    """

    def __init__(
        self,
        image: str,
        tag: str,
        python_dependencies: list = None,
        secrets: Iterable[Secret] = None,
    ) -> None:
        if tag is None:
            tag = image

        self._image = image
        self._tag = tag
        self._python_dependencies = python_dependencies
        self._client = docker.from_env()
        self.last_container_id = None

        super().__init__(secrets=secrets)

    @property
    def python_dependencies(self) -> list:
        """Get the specified Python dependencies"""
        return self._python_dependencies

    @property
    def image(self) -> str:
        """Get the container's base image"""
        return self._image

    @property
    def tag(self) -> str:
        """Get the container's tag"""
        return self._tag

    @property
    def client(self) -> "docker.client.DockerClient":
        """Get the environment's client"""
        return self._client

    def build(self, flow) -> tuple:
        """Build the Docker container

        Args:
            - flow (prefect.Flow): Flow to be placed in container

        Returns:
            - tuple: tuple consisting of (`docker.models.images.Image`, iterable logs)
        """
        with tempfile.TemporaryDirectory() as tempdir:

            # Write temp file of serialized registry to same location of Dockerfile
            serialized_registry = LocalEnvironment().build(flow)
            self.serialized_registry_to_file(
                serialized_registry=serialized_registry, directory=tempdir
            )

            self.pull_image()
            self.create_dockerfile(directory=tempdir)

            container = self.client.images.build(
                path=tempdir, tag=self.tag, forcerm=True
            )

            return container

    def run(self, key: bytes, cli_cmd: str) -> None:
        """Run a command in the Docker container

        Args:
            - cli_cmd (str, optional): An initial cli_cmd that will be executed on container run

        Returns:
            - `docker.models.containers.Container` object

        """
        running_container = self.client.containers.run(
            self.tag, command=cli_cmd, detach=True
        )
        self.last_container_id = running_container.id

        return running_container

    def pull_image(self) -> None:
        """Pull the image specified so it can be built.

        In order for the docker python library to use a base image it must be pulled
        from either the main docker registry or a separate registry that must be set in
        the environment variables.
        """
        self.client.images.pull(self.image)

    def create_dockerfile(self, directory: str = None) -> None:
        """Creates a dockerfile to use as the container.

        In order for the docker python library to build a container it needs a
        Dockerfile that it can use to define the container. This function takes the
        image and python_dependencies then writes them to a file called Dockerfile.

        Args:
            - directory (str, optional): A directory where the Dockerfile will be created,
                if no directory is specified is will be created in the current working directory

        Returns:
            - None
        """
        path = "{}/Dockerfile".format(directory)
        with open(path, "w+") as dockerfile:

            # Generate RUN pip install commands for python dependencies
            pip_installs = ""
            if self.python_dependencies:
                pip_installs = r"RUN pip install " + " \\\n".join(
                    self.python_dependencies
                )

            # Generate the creation of environment variables from Secrets
            env_vars = ""
            if self.secrets:
                for secret in self.secrets:
                    env_vars += "ENV {}={}\n".format(secret.name, secret.value)

            # Due to prefect being a private repo it currently will require a
            # personal access token. Once pip installable this will change and there won't
            # be a need for the personal access token or git anymore.
            # Note: this currently prevents alpine images from being used

            file_contents = textwrap.dedent(
                """\
                FROM {}

                RUN pip install pip --upgrade
                RUN pip install wheel
                {}
                {}

                RUN apt-get -qq -y update && apt-get -qq -y install --no-install-recommends --no-install-suggests git

                COPY registry ./registry

                ENV PREFECT__REGISRTY__STARTUP_REGISTRY_PATH ./registry

                RUN git clone https://$PERSONAL_ACCESS_TOKEN@github.com/PrefectHQ/prefect.git
                RUN pip install prefect
            """.format(
                    self.image, env_vars, pip_installs
                )
            )

            dockerfile.write(file_contents)

    def serialized_registry_to_file(
        self, serialized_registry: bytes, directory: str = None
    ) -> None:
        """
        Write a serialized registry to a temporary file so it can be added to the container

        Args:
            - serialized_registry (bytes): The encrypted and pickled flow registry
            - directory (str, optional): A directory where the Dockerfile will be created,
            if no directory is specified is will be created in the current working directory

        Returns:
            - None
        """
        path = "{}/registry".format(directory)
        with open(path, "wb+") as registry_file:
            registry_file.write(serialized_registry)


class LocalEnvironment(Environment):
    """
    An environment for running a flow locally.

    Args:
        - encryption_key (str, optional): a Fernet encryption key. One will be generated
        automatically if None is passed.
    """

    def __init__(self, encryption_key: str = None):
        self.encryption_key = encryption_key or Fernet.generate_key().decode()

    def build(self, flow: "prefect.Flow") -> bytes:
        """
        Build the LocalEnvironment

        Args:
            - flow (Flow): The prefect Flow object to build the environment for

        Returns:
            - bytes: The encrypted and pickled flow registry
        """
        registry = {}
        flow.register(registry=registry)
        serialized = prefect.core.registry.serialize_registry(
            registry=registry, include_ids=[flow.id], encryption_key=self.encryption_key
        )
        return serialized

    def run(self, key: bytes, cli_cmd: str):
        """
        Run a command in the `LocalEnvironment`. This functions by writing a pickled
        flow to temporary memory and then executing prefect CLI commands against it.

        Args:
            - key (bytes): The encrypted and pickled flow registry
            - cli_cmd (str): The prefect CLI command to be run

        Returns:
            - bytes: the output of `subprocess.check_output` from the command run against the flow
        """
        with tempfile.NamedTemporaryFile() as tmp:
            with open(tmp.name, "wb") as f:
                f.write(key)

            env = [
                'PREFECT__REGISTRY__STARTUP_REGISTRY_PATH="{}"'.format(tmp.name),
                'PREFECT__REGISTRY__ENCRYPTION_KEY="{}"'.format(self.encryption_key),
            ]

            return subprocess.check_output(" ".join(env + [cli_cmd]), shell=True)
