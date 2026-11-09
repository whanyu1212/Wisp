"""Decode and check the images an RPC command attaches to a user message."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence

from wisp.events import ImagePart
from wisp.images import ImageRejectedError, inspect_image
from wisp.sessions.images import SessionImageError
from wisp.sessions.jsonl import JsonlSession


class RpcImageRejectedError(ValueError):
    """An attached image that fails decoding or Wisp's image checks.

    The message names the image's 1-based position and the failed check, such
    as ``Image 2 rejected (too_large): ...``, so frontends can show which one.
    """


def decode_rpc_images(
    attachments: Sequence[str | ImagePart], *, session: JsonlSession | None
) -> tuple[bytes, ...]:
    """Turn a command's image attachments into checked image bytes, in order.

    A base64 attachment is decoded; a reference is read back from the session's
    image store, which verifies its digest and size. Only the bytes go on: the
    session derives each image's format and dimensions again from them, so a
    reference's other fields are never trusted. Checking here, before a run or
    queue mutation starts, rejects the whole command without leaving any of its
    images or its message behind.

    Args:
        attachments (Sequence[str | ImagePart]): Base64 image files and stored
            image references, in message order.
        session (JsonlSession | None): The selected session, whose store holds
            referenced images; None when no session is selected.

    Returns:
        tuple[bytes, ...]: The image files, in the same order.

    Raises:
        RpcImageRejectedError: An attachment is not strict base64, names an image
            that is not stored, or fails ``wisp.images.inspect_image``.
    """

    decoded: list[bytes] = []
    for position, attachment in enumerate(attachments, start=1):
        if isinstance(attachment, ImagePart):
            decoded.append(_stored_image(position, attachment, session))
            continue
        try:
            image = base64.b64decode(attachment, validate=True)
        except binascii.Error as exc:
            raise RpcImageRejectedError(
                f"Image {position} rejected (invalid_base64): it is not valid base64"
            ) from exc
        try:
            inspect_image(image)
        except ImageRejectedError as exc:
            raise RpcImageRejectedError(f"Image {position} rejected ({exc.reason}): {exc}") from exc
        decoded.append(image)
    return tuple(decoded)


def _stored_image(position: int, image: ImagePart, session: JsonlSession | None) -> bytes:
    if session is None:
        raise RpcImageRejectedError(
            f"Image {position} rejected (not_stored): no session is selected to hold it"
        )
    try:
        return session.images.read(image)
    except SessionImageError as exc:
        raise RpcImageRejectedError(f"Image {position} rejected (not_stored): {exc}") from exc
