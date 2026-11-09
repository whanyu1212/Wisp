"""Decode and check the images an RPC command attaches to a user message."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence

from wisp.images import ImageRejectedError, inspect_image


class RpcImageRejectedError(ValueError):
    """An attached image that fails decoding or Wisp's image checks.

    The message names the image's 1-based position and the failed check, such
    as ``Image 2 rejected (too_large): ...``, so frontends can show which one.
    """


def decode_rpc_images(encoded: Sequence[str]) -> tuple[bytes, ...]:
    """Decode base64 image attachments and check each against Wisp's limits.

    Checking here, before a run or queue mutation starts, rejects the whole
    command without leaving any of its images or its message behind.

    Args:
        encoded (Sequence[str]): Base64-encoded image files, in message order.

    Returns:
        tuple[bytes, ...]: The decoded image files, in the same order.

    Raises:
        RpcImageRejectedError: An attachment is not strict base64 or fails
            ``wisp.images.inspect_image``.
    """

    decoded: list[bytes] = []
    for position, data in enumerate(encoded, start=1):
        try:
            image = base64.b64decode(data, validate=True)
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
