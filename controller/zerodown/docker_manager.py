"""Boundary for Docker image builds and candidate-container lifecycle."""

from typing import Protocol


class DockerManager(Protocol):
    def build_image(self, build_context: str, dockerfile: str, tag: str) -> str:
        """Build an image and return its image identifier."""
        ...

    def start_container(
        self,
        image: str,
        name: str,
        port: int,
        network: str,
    ) -> str:
        """Start a candidate container and return its container identifier."""
        ...

    def remove_container(self, name: str) -> None:
        """Remove a container after promotion or rollback."""
        ...
