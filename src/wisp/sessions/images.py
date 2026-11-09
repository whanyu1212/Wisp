"""Content-addressed storage for the images that session messages reference.

Each image is one private file named by the SHA-256 digest of its bytes, in an
``images`` directory beside the session files. Every session in that directory
shares it, so cloned and forked sessions resolve the same references and an
image attached twice is stored once.

Ordering invariant: an image must be durable here before any session entry that
references it is appended, so a crash never leaves an entry pointing at an image
that was not written. ``JsonlSession`` checks this before each append.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from uuid import uuid4

from wisp.events import ImagePart
from wisp.images import inspect_image
from wisp.sessions.errors import SessionError
from wisp.sessions.file_io import (
    PRIVATE_FILE_MODE,
    ensure_private_directory,
    read_exact,
    sync_directory,
    sync_file,
    unlink_if_same_file,
    write_all,
)

IMAGE_DIRECTORY_NAME = "images"


class SessionImageError(SessionError):
    """A referenced image is missing, unreadable, or no longer matches its digest."""


class SessionImageStore:
    """Writes and reads image files addressed by their SHA-256 digest."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def store(self, data: bytes) -> ImagePart:
        """Validate an image and make it durable, once per distinct content.

        Writing the same bytes again leaves the existing file in place. A file
        under the digest's name that no longer matches is replaced.

        Args:
            data (bytes): The complete image file.

        Returns:
            ImagePart: The reference to record on a message.

        Raises:
            ImageRejectedError: The bytes fail ``wisp.images.inspect_image``.
            SessionError: The image directory or file could not be written.
        """

        image = inspect_image(data)
        if self._is_intact(image):
            # Another process may have renamed this file in without syncing the
            # directory yet; sync it before the caller references the image.
            self._sync_directory()
            return image
        created_directory = not self.directory.exists()
        ensure_private_directory(self.directory)
        if created_directory:
            # Make the new directory's own entry durable, as appends do for new
            # session files, or a crash could keep a reference and lose the image.
            self._sync_parent_directory()
        target = self._path(image)
        temp_path = self.directory / f".{image.sha256}.{uuid4().hex}.tmp"
        try:
            fd = os.open(temp_path, _create_flags(), PRIVATE_FILE_MODE)
        except OSError as exc:
            raise SessionError(f"Could not create image file: {temp_path}") from exc
        signature: tuple[int, int] | None = None
        try:
            info = os.fstat(fd)
            signature = (info.st_dev, info.st_ino)
            if os.name == "posix":
                os.fchmod(fd, PRIVATE_FILE_MODE)
            write_all(fd, data)
            sync_file(fd)
            os.close(fd)
            fd = -1
            # Renaming replaces the name itself, never a file a symlink points to.
            os.replace(temp_path, target)
            sync_directory(self.directory)
        except OSError as exc:
            raise SessionError(f"Could not store image {image.sha256}: {target}") from exc
        finally:
            if fd != -1:
                os.close(fd)
            if signature is not None:
                # After a successful rename the temporary name no longer exists.
                unlink_if_same_file(temp_path, signature)
        return image

    def read(self, image: ImagePart) -> bytes:
        """Return an image's bytes after checking them against its digest.

        Args:
            image (ImagePart): A reference recorded on a message.

        Returns:
            bytes: Exactly the bytes that were stored.

        Raises:
            SessionImageError: The file is missing, not a regular file, or its
                size or digest differs from the reference.
        """

        path = self._path(image)
        try:
            fd = os.open(path, _read_flags())
        except FileNotFoundError as exc:
            raise SessionImageError(f"Image {image.sha256} is missing: {path}") from exc
        except OSError as exc:
            raise SessionImageError(f"Could not open image {image.sha256}: {path}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SessionImageError(f"Image {image.sha256} is not a regular file: {path}")
            if info.st_size != image.byte_size:
                raise SessionImageError(
                    f"Image {image.sha256} is {info.st_size} bytes, "
                    f"expected {image.byte_size}: {path}"
                )
            try:
                data = read_exact(fd, image.byte_size)
            except OSError as exc:
                raise SessionImageError(f"Could not read image {image.sha256}: {path}") from exc
        finally:
            os.close(fd)
        if hashlib.sha256(data).hexdigest() != image.sha256:
            raise SessionImageError(f"Image {image.sha256} does not match its digest: {path}")
        return data

    def contains(self, image: ImagePart) -> bool:
        """Return whether a regular file of the referenced size is stored.

        This is the cheap check run before appending a reference; ``read``
        verifies the digest.

        Args:
            image (ImagePart): The reference to look up.

        Returns:
            bool: True when the file exists with the expected size.
        """

        try:
            info = self._path(image).lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SessionImageError(
                f"Could not inspect image {image.sha256}: {self._path(image)}"
            ) from exc
        return stat.S_ISREG(info.st_mode) and info.st_size == image.byte_size

    def _is_intact(self, image: ImagePart) -> bool:
        if not self.contains(image):
            return False
        try:
            self.read(image)
        except SessionImageError:
            return False
        return True

    def _path(self, image: ImagePart) -> Path:
        return self.directory / image.sha256

    def _sync_directory(self) -> None:
        try:
            sync_directory(self.directory)
        except OSError as exc:
            raise SessionError(f"Could not sync image directory: {self.directory}") from exc

    def _sync_parent_directory(self) -> None:
        try:
            sync_directory(self.directory.parent)
        except OSError as exc:
            raise SessionError(
                f"Could not sync session directory: {self.directory.parent}"
            ) from exc


def _create_flags() -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _read_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    # Opening a FIFO would otherwise block until a writer appears; the regular-file
    # check after opening then rejects it. Reads of regular files ignore the flag.
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags
